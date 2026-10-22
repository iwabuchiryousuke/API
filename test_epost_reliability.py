"""Regression coverage for the failures observed in the supplied October data.

Browser fixtures route every request locally. They do not contact SBI.
"""
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from epost_collector import (
    DISCOVER, FIND_VIEW, _DocumentCache, _ResponseCapture, _resolve_index,
    _verify_document, collect, is_post_url, save_pdf, take_pdf, validate_pdf,
)
from login_credentials import CredentialError


def pdf_bytes(label='one'):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({'/Title': label})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def item(key='one', title='取引報告書', date='2026/10/01', **extra):
    return dict(key=key, index=0, date=date, title=title, **extra)


class ArtifactSafety(unittest.TestCase):
    def test_structural_validation_and_content_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            first, created = save_pdf(folder, 'C', '2026/10/01', '取引報告書', pdf_bytes())
            second, duplicate = save_pdf(folder, 'C', '2026/10/01', '取引報告書', pdf_bytes())
            self.assertTrue(created)
            self.assertFalse(duplicate)
            self.assertEqual(first, second)
            self.assertEqual(len(list(first.parent.glob('*.pdf'))), 1)
            self.assertEqual(len((first.parent/'保存一覧.jsonl').read_text().splitlines()), 1)
        for raw in (b'%PDF-test', b'<html>login</html>', pdf_bytes()[:-20]):
            with self.subTest(raw=raw[:12]):
                with self.assertRaises(ValueError):
                    validate_pdf(raw)

    def response(self, raw, url='https://plus.e-shishobako.ne.jp/document/1'):
        return Mock(url=url, headers={'content-type': 'application/pdf'}, body=Mock(return_value=raw))

    def test_foreign_pdf_not_accepted_and_multiple_artifacts_rejected(self):
        self.assertIsNone(take_pdf([self.response(pdf_bytes(), 'https://example.com/x.pdf')], [], []))
        with self.assertRaisesRegex(ValueError, '複数'):
            take_pdf([self.response(pdf_bytes('one')), self.response(pdf_bytes('two'))], [], [])

    def test_late_previous_request_not_associated_with_new_card(self):
        capture = _ResponseCapture()
        old, new = Mock(), Mock()
        capture.begin()
        capture.request(old)
        capture.finish()
        capture.begin()
        capture.request(new)
        capture.response(Mock(request=old, url='https://plus.e-shishobako.ne.jp/old.pdf'))
        self.assertEqual(list(capture), [])
        response = Mock(request=new, url='https://plus.e-shishobako.ne.jp/new.pdf')
        capture.response(response)
        self.assertEqual(list(capture), [response])

    def test_response_from_an_unrelated_trusted_tab_is_ignored(self):
        owner = Mock()
        unrelated = Mock()
        unrelated.opener.return_value = None
        capture = _ResponseCapture(owner)
        request = Mock()
        request.frame.page = unrelated
        capture.begin()
        capture.request(request)
        capture.response(Mock(request=request, url='https://plus.e-shishobako.ne.jp/other.pdf'))
        self.assertEqual(list(capture), [])

    def test_late_previous_download_and_unattributed_blob_are_not_accepted(self):
        capture = _ResponseCapture()
        old = Mock(url='https://plus.e-shishobako.ne.jp/old.pdf')
        capture.begin()
        capture.request(old)
        capture.finish()
        capture.begin()
        capture.request(Mock(url='https://plus.e-shishobako.ne.jp/new.pdf'))
        self.assertFalse(capture.allows_download(Mock(url=old.url)))
        self.assertFalse(capture.allows_download(Mock(url='blob:https://plus.e-shishobako.ne.jp/unknown')))
        self.assertTrue(capture.allows_download(Mock(url='https://plus.e-shishobako.ne.jp/new.pdf')))
        capture.begin('blob:https://plus.e-shishobako.ne.jp/current-card')
        self.assertTrue(capture.allows_download(Mock(url='blob:https://plus.e-shishobako.ne.jp/current-card')))

    def test_download_only_reused_url_cannot_identify_a_different_document(self):
        capture = _ResponseCapture()
        url = 'https://plus.e-shishobako.ne.jp/shared-download'
        capture.begin(url, document_key='first')
        self.assertTrue(capture.allows_download(Mock(url=url)))
        capture.finish()
        capture.begin(url, document_key='second')
        capture.request(Mock(url=url))
        self.assertFalse(capture.allows_download(Mock(url=url)))

    def test_exact_completed_pdf_request_replay_uses_auth_context_without_redirects(self):
        context = Mock()
        capture = _ResponseCapture(context=context)
        request = Mock(url='https://plus.e-shishobako.ne.jp/document/one', method='POST')
        response = Mock(request=request, url=request.url, status=200,
                        headers={'content-type':'application/pdf'}, body=Mock(return_value=b''))
        downloaded = Mock(url=request.url, status=200,
                          headers={'content-type':'application/pdf'}, body=Mock(return_value=pdf_bytes()))
        context.request.fetch.return_value = downloaded
        capture.begin(document_key='one')
        capture.request(request)
        capture.response(response)
        capture.replay_missing()
        context.request.fetch.assert_not_called()
        capture.completed(request)
        capture.replay_missing()
        context.request.fetch.assert_called_once_with(request, timeout=7000, max_redirects=0, fail_on_status_code=False)
        self.assertEqual(take_pdf(capture, [], []), pdf_bytes())
        downloaded.dispose.assert_called_once()

    def test_replay_requires_successful_pdf_evidence_and_safe_method(self):
        for status, mime, method in [(401,'application/pdf','GET'), (200,'text/html','POST'), (200,'application/pdf','DELETE')]:
            with self.subTest(status=status,mime=mime,method=method):
                context=Mock()
                capture=_ResponseCapture(context=context)
                request=Mock(url='https://plus.e-shishobako.ne.jp/document/one', method=method)
                response=Mock(request=request,url=request.url,status=status,headers={'content-type':mime})
                capture.begin(document_key='one');capture.request(request);capture.response(response);capture.completed(request)
                capture.replay_missing()
                context.request.fetch.assert_not_called()

    def test_recycled_dom_key_with_different_stable_id_is_not_reused(self):
        page = Mock()
        page.evaluate.side_effect = [None, [item('same-dom-key', stable_id='source:B', fingerprint='same header')]]
        self.assertEqual(_resolve_index(page, item('same-dom-key', stable_id='source:A', fingerprint='same header')), -1)

    def test_no_date_title_occurrence_guess_when_card_disappears(self):
        page = Mock()
        page.evaluate.side_effect = [None, [item('different-key', fingerprint='different full metadata')]]
        self.assertEqual(_resolve_index(page, item('original', fingerprint='original metadata', occurrence=0)), -1)

    def test_url_uses_real_trusted_https_route(self):
        self.assertTrue(is_post_url('https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery'))
        for url in ['http://plus.e-shishobako.ne.jp/#/user-delivery',
                    'https://plus.e-shishobako.ne.jp/?redirect=user-delivery',
                    'https://plus.e-shishobako.ne.jp.evil.example/#/user-delivery',
                    'https://user:pass@plus.e-shishobako.ne.jp/#/user-delivery']:
            self.assertFalse(is_post_url(url))

    def test_clear_body_title_mismatch_rejected_but_incidental_words_allowed(self):
        document = Mock()
        document.__enter__ = Mock(return_value=document)
        document.__exit__ = Mock(return_value=False)
        document.pages = [Mock()]
        with patch('pdfplumber.open', return_value=document):
            document.pages[0].extract_text.return_value = '取引残高報告書 STWHZB 外国債券 配当金'
            self.assertIn('照合', _verify_document(pdf_bytes(), item(title='取引残高報告書')))
            with self.assertRaisesRegex(ValueError, '一致しません'):
                _verify_document(pdf_bytes(), item(title='株式取引報告書'))
            document.pages[0].extract_text.return_value = '外国債券取引報告書 利金 配当金'
            self.assertIn('照合', _verify_document(pdf_bytes(), item(title='取引報告書')))


class CacheSafety(unittest.TestCase):
    def test_only_new_stable_source_ids_and_verified_bytes_can_skip(self):
        with tempfile.TemporaryDirectory() as folder:
            path, _ = save_pdf(folder, 'C', '2026/10/01', '取引報告書', pdf_bytes())
            (path.parent/'取得結果_old.json').write_text(json.dumps({'documents':[{'file':path.name}]}))
            cache = _DocumentCache(folder, 'C')
            stable = item(stable_id='data-document-id:one', fingerprint='whole source metadata')
            self.assertIsNone(cache.get(stable))
            cache.put(stable, path, pdf_bytes())
            self.assertEqual(cache.get(stable)[0], path)
            self.assertIsNone(_DocumentCache(folder, 'A').get(stable))
            self.assertIsNone(cache.get(dict(stable, fingerprint='changed metadata')))
            self.assertIsNone(cache.get(dict(stable, stable_id='')))
            path.write_bytes(pdf_bytes('changed'))
            self.assertIsNone(cache.get(stable))


class CollectionIntegrity(unittest.TestCase):
    def page(self, total):
        page = Mock()
        page.locator.return_value.inner_text.return_value = f'全{total}件'
        page.locator.return_value.count.return_value = 0
        page.get_by_role.return_value.count.return_value = 0
        return page

    def test_same_pdf_for_different_cards_is_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            page = self.page(2)
            ctx = Mock()
            candidates = [item('one'), item('two', date='2026/09/01')]
            with patch('epost_collector.discover_all', return_value=candidates), patch('epost_collector._retrieve', return_value=(pdf_bytes(), 0)):
                self.assertEqual(collect(page, ctx, 'C', folder, progress=lambda _:None), (1, 0, 2))
            audit = json.loads(next((Path(folder)/'C').glob('取得結果_*.json')).read_text())
            self.assertFalse(audit['complete'])
            self.assertEqual(audit['documents'][1]['status'], '取得失敗')
            self.assertEqual(audit['documents'][0]['status'], '要確認')
            self.assertFalse((Path(folder)/'C'/'取得キャッシュ_v1.json').exists())
            self.assertEqual(ctx.remove_listener.call_count, 4)

    def test_verified_zero_screen_count_and_empty_list_is_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('epost_collector.discover_all', return_value=[]):
                self.assertEqual(collect(self.page(0), Mock(), 'C', folder, progress=lambda _:None), (0, 0, 0))
            audit = json.loads(next((Path(folder)/'C').glob('取得結果_*.json')).read_text())
            self.assertTrue(audit['complete'])

    def test_expected_count_mismatch_cannot_report_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            page = self.page(3)
            with patch('epost_collector.discover_all', return_value=[item()]), patch('epost_collector._retrieve', return_value=(pdf_bytes(), 0)):
                self.assertEqual(collect(page, Mock(), 'B', folder, progress=lambda _:None), (1, 0, 2))
            audit = json.loads(next((Path(folder)/'B').glob('取得結果_*.json')).read_text())
            self.assertFalse(audit['complete'])
            self.assertEqual(audit['unresolved'], 2)

    def test_cancel_keeps_partial_evidence_and_removes_listeners(self):
        with tempfile.TemporaryDirectory() as folder:
            page, ctx = self.page(1), Mock()
            cancelled = Mock(side_effect=[False, False, True])
            with patch('epost_collector.discover_all', return_value=[item()]):
                with self.assertRaises(CredentialError):
                    collect(page, ctx, 'C', folder, progress=lambda _:None, cancelled=cancelled)
            self.assertEqual(ctx.remove_listener.call_count, 4)
            audit = json.loads(next((Path(folder)/'C').glob('取得結果_*.json')).read_text())
            self.assertFalse(audit['complete'])


HTML = '''<!doctype html><meta charset="utf-8"><style>
body{font-family:sans-serif}mat-expansion-panel{display:block;border:1px solid;margin:8px;padding:14px}
[role=button]{display:block;padding:8px;cursor:pointer}.pdf{display:none} .open .pdf{display:inline-block}
.cdk-overlay-transparent-backdrop{position:fixed;inset:0;z-index:999;background:transparent}
</style><div>全<span id="count">COUNT</span>件</div><main id="cards"></main><nav aria-label="ページ"><button id="next" onclick="advance()">次へ</button></nav>
<script>
let pageNumber=0;const pages=PAGES;
function render(){cards.innerHTML=pages[pageNumber].map(d=>`<mat-expansion-panel data-document-id="${d.id}"><div role="button" aria-expanded="false" onclick="this.parentNode.classList.toggle('open');this.setAttribute('aria-expanded',this.parentNode.classList.contains('open'))"><span>${d.date}</span><span>${d.title}</span><span>有効期限2032/03/31</span><span>+</span></div><button aria-label="スター" onclick="window.starClicks=(window.starClicks||0)+1">☆</button><button class="pdf" onclick="downloadPdf('${d.id}')">PDFファイル</button></mat-expansion-panel>`).join('');next.disabled=pageNumber===pages.length-1;}
function advance(){pageNumber++;render()}
async function downloadPdf(id){const response=await fetch('/document/'+id);const blob=await response.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=id+'.pdf';a.click()}
window.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('.cdk-overlay-transparent-backdrop').forEach(x=>x.remove())});render();
</script>'''


class BrowserCardFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.manager = sync_playwright().start()
        from browser_test_support import browser_executable
        options = {'headless': True, 'executable_path': browser_executable(cls.manager)}
        try:
            cls.browser = cls.manager.chromium.launch(**options)
        except Exception:
            cls.manager.stop()
            raise
    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.manager.stop()
    def setUp(self):
        self.context = self.browser.new_context(accept_downloads=True)
        self.addCleanup(self.context.close)
        self.requests = []
        self.replays = []
        def replay(request, **options):
            self.assertTrue(request.url.startswith('https://plus.e-shishobako.ne.jp/document/'))
            self.assertEqual(request.method, 'GET')
            self.assertEqual(options['max_redirects'], 0)
            self.assertEqual(options['timeout'], 7000)
            self.replays.append(request)
            # APIRequestContext requests do not use browser route handlers. This
            # stub prevents all live network and returns this exact source ID.
            raw = pdf_bytes(request.url.rsplit('/', 1)[-1])
            return Mock(status=200, url=request.url, headers={'content-type':'application/pdf'}, body=Mock(return_value=raw))
        self.context.request.fetch = replay
    def fixture(self, pages, overlay=False):
        html = HTML.replace('COUNT',str(sum(map(len,pages)))).replace('PAGES',json.dumps(pages,ensure_ascii=False))
        def route(request):
            url = request.request.url
            self.requests.append(url)
            if '/document/' in url:
                label=url.rsplit('/',1)[-1]
                request.fulfill(status=200,content_type='application/pdf',body=pdf_bytes(label))
            elif url.endswith('/#/user-delivery') or '/dp_apl/usr/' in url:
                request.fulfill(status=200,content_type='text/html',body=html)
            else:
                request.abort()
        self.context.route('**/*',route)
        page=self.context.new_page()
        page.goto('https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery')
        if overlay:
            page.evaluate("document.body.insertAdjacentHTML('beforeend','<div class=\"cdk-overlay-transparent-backdrop cdk-overlay-backdrop-showing\"></div>')")
        return page
    def record(self, identity, title='取引報告書', date='2026/10/01'):
        return dict(id=identity,title=title,date=date)
    def test_distinct_identical_cards_and_unknown_notices_not_collapsed(self):
        page=self.fixture([[self.record('one'),self.record('two'),self.record('unknown','汎用累投売買報告書')]])
        cards=page.evaluate(DISCOVER)
        self.assertEqual(len(cards),3)
        self.assertEqual(len({x['key'] for x in cards}),3)
        self.assertEqual(cards[2]['title'],'汎用累投売買報告書')
        self.assertTrue(all(c['mode']=='expand' for c in cards))
    def test_recycled_node_changes_key_when_stable_source_id_changes(self):
        page=self.fixture([[self.record('one')]])
        first=page.evaluate(DISCOVER)[0]
        page.locator('mat-expansion-panel').evaluate("el=>el.setAttribute('data-document-id','two')")
        second=page.evaluate(DISCOVER)[0]
        self.assertNotEqual(first['key'],second['key'])
        self.assertEqual(_resolve_index(page,first),-1)

    def test_scoped_pdf_buttons_overlay_recovery_and_cache(self):
        page=self.fixture([[self.record('one'),self.record('two')]],overlay=True)
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(collect(page,self.context,'C',folder,progress=lambda _:None),(2,0,0))
            first_requests=len([r for r in self.requests if '/document/' in r])
            self.assertEqual(first_requests,2)
            self.assertEqual(page.evaluate('window.starClicks||0'),0)
            self.assertEqual(collect(page,self.context,'C',folder,progress=lambda _:None),(0,2,0))
            self.assertEqual(len([r for r in self.requests if '/document/' in r]),2)
            self.assertEqual(len(list((Path(folder)/'C').glob('*.pdf'))),2)
    def test_reads_next_page_and_reconciles_global_count(self):
        page=self.fixture([[self.record('one')],[self.record('two')]])
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(collect(page,self.context,'C',folder,progress=lambda _:None),(2,0,0))
            report=json.loads(next((Path(folder)/'C').glob('取得結果_*.json')).read_text())
            self.assertEqual(report['pages'],2)
            self.assertEqual(report['discovered'],2)
            self.assertTrue(report['complete'])


if __name__ == '__main__':
    unittest.main()
