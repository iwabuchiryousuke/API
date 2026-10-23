"""Real Chromium fixtures for new-only PDF requests without source IDs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from epost_collector import DISCOVER, collect
import test_epost_reliability as fixtures


class IncrementalArchiveBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        from browser_test_support import browser_executable
        cls.manager = sync_playwright().start()
        cls.browser = cls.manager.chromium.launch(executable_path=browser_executable(cls.manager), headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.manager.stop()

    def setUp(self):
        self.context = self.browser.new_context(accept_downloads=True)
        self.addCleanup(self.context.close)
        self.requests = []
        self.pages = []
        self.icons = False
        self.context.request.fetch = lambda request, **options: Mock(
            status=200, url=request.url, headers={'content-type': 'application/pdf'},
            body=lambda: fixtures.pdf_bytes(request.url.rsplit('/', 1)[-1]))

        def route(control):
            url = control.request.url
            if '/document/' in url:
                self.requests.append(url.rsplit('/', 1)[-1])
                control.fulfill(status=200, content_type='application/pdf',
                                body=fixtures.pdf_bytes(self.requests[-1]))
            elif '/dp_apl/usr/' in url:
                html = fixtures.HTML.replace('COUNT', str(sum(map(len, self.pages))))
                html = html.replace('PAGES', json.dumps(self.pages, ensure_ascii=False))
                html = html.replace(' data-document-id="${d.id}"', '')
                if self.icons:
                    html = html.replace('<span>${d.date}</span>', '<mat-icon>drafts</mat-icon><span>${d.date}</span>')
                    html = html.replace('<span>+</span>', '<mat-icon>star_border</mat-icon><span>+</span>')
                control.fulfill(status=200, content_type='text/html', body=html)
            else:
                control.abort()
        self.context.route('**/*', route)
        self.page = self.context.new_page()

    def show(self, pages, query=''):
        self.pages = pages
        address = 'https://plus.e-shishobako.ne.jp/dp_apl/usr/' + query + '#/user-delivery'
        if self.page.url == address:
            self.page.reload()
        else:
            self.page.goto(address)

    @staticmethod
    def item(identity, day='2026/10/01', title='取引報告書'):
        return dict(id=identity, date=day, title=title)

    def test_recreated_nodes_reuse_existing_headers_and_only_new_card_downloads(self):
        first = [self.item('one'), self.item('two', '2026/10/02')]
        self.show([first])
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (2, 0, 0))
            self.assertEqual(self.requests, ['one', 'two'])
            self.show([[self.item('three', '2026/10/03'), *first]])
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (1, 2, 0))
            self.assertEqual(self.requests, ['one', 'two', 'three'])
            reports = sorted((Path(folder) / 'A').glob('取得結果_*.json'))
            report = json.loads(reports[-1].read_text(encoding='utf-8'))
            self.assertEqual(report['downloaded_pdf_files'], 1)
            self.assertEqual(report['reused_pdf_files'], 2)
            self.assertTrue(report['complete'])
            self.assertTrue(report['heading_reuse_enabled'])

    def test_new_same_day_same_title_does_not_hide_behind_old_header(self):
        first = [self.item('one'), self.item('other', '2026/10/02')]
        self.show([first])
        with tempfile.TemporaryDirectory() as folder:
            collect(self.page, self.context, 'A', folder, expected_owner='Example A', progress=lambda _: None)
            self.requests.clear()
            self.show([[self.item('new-twin'), *first]])
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (1, 2, 0))
            self.assertEqual(self.requests, ['new-twin', 'one'])

    def test_multiple_pages_do_not_guess_unique_headers_from_first_page(self):
        self.show([[self.item('one')], [self.item('two', '2026/10/02')]])
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(collect(self.page, self.context, 'A', folder, progress=lambda _: None), (2, 0, 0))
            self.requests.clear()
            self.show([[self.item('one')], [self.item('two', '2026/10/02')]])
            messages = []
            self.assertEqual(collect(self.page, self.context, 'A', folder, progress=messages.append), (0, 2, 0))
            self.assertEqual(self.requests, ['one', 'two'])
            self.assertTrue(any('複数ページ' in message for message in messages))

    def test_unknown_heading_without_report_keyword_is_retained_and_reused(self):
        self.show([[self.item('terms', title='口座約款の改定について')]])
        cards = self.page.evaluate(DISCOVER)
        self.assertEqual(cards[0]['title'], '口座約款の改定について')
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (1, 0, 0))
            self.show([[self.item('terms', title='口座約款の改定について')]])
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (0, 1, 0))
            self.assertEqual(self.requests, ['terms'])

    def test_explicit_refresh_bypasses_both_indexes_and_retrieves_existing_pdf(self):
        self.show([[self.item('one')]])
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (1, 0, 0))
            self.show([[self.item('one')]])
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None,
                                     force_download=True), (0, 1, 0))
            self.assertEqual(self.requests, ['one', 'one'])
            report = json.loads(sorted((Path(folder) / 'A').glob('取得結果_*.json'))[-1].read_text(encoding='utf-8'))
            self.assertTrue(report['force_download'])
            self.assertEqual(report['reused_pdf_files'], 0)
            self.assertEqual(report['downloaded_pdf_files'], 1)

    def test_material_icons_are_not_an_unknown_documents_title(self):
        self.icons = True
        self.show([[self.item('terms', title='口座約款の改定について')]])
        self.assertEqual(self.page.evaluate(DISCOVER)[0]['title'], '口座約款の改定について')

    def test_foreign_trade_heading_is_not_collapsed_to_generic_domestic_trade(self):
        self.show([[self.item('foreign', title='外国株式等取引報告書')]])
        self.assertEqual(self.page.evaluate(DISCOVER)[0]['title'], '外国株式等取引報告書')

    def test_distinct_filter_scopes_cannot_reuse_a_matching_heading(self):
        self.show([[self.item('first')]], '?view=one')
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (1, 0, 0))
            self.show([[self.item('different-source')]], '?view=two')
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (1, 0, 0))
            self.assertEqual(self.requests, ['first', 'different-source'])
            value = json.loads((Path(folder) / 'A' / '取得見出し索引_v2.json').read_text(encoding='utf-8'))
            self.assertNotIn('view=one', json.dumps(value))
            self.assertNotIn('view=two', json.dumps(value))

    def test_english_header_correction_is_not_erased_as_an_icon_substring(self):
        self.show([[self.item('first', title='取引報告書 address')]])
        with tempfile.TemporaryDirectory() as folder:
            collect(self.page, self.context, 'A', folder, expected_owner='Example A', progress=lambda _: None)
            self.show([[self.item('corrected', title='取引報告書 ress')]])
            self.assertEqual(collect(self.page, self.context, 'A', folder,
                                     expected_owner='Example A', progress=lambda _: None), (1, 0, 0))
            self.assertEqual(self.requests, ['first', 'corrected'])


if __name__ == '__main__':
    unittest.main()
