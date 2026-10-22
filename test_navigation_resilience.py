"""Regression cases for post navigation; browser cases use intercepted fixtures.

These tests do not log into SBI and do not establish live-site compatibility.
Set SBI_TEST_BROWSER to a Chromium executable for the browser cases.
"""
import os
import shutil
import unittest
from unittest.mock import Mock

from login_credentials import CredentialError
from sbi_navigation import controls, open_post, scan, select_rows


def row(label,index=0,domestic=False,href=''):
    return dict(labels=[label],index=index,domestic=domestic,href=href,visible=True,enabled=True)


def page(url='https://site.sbisec.co.jp/account'):
    p=Mock();p.url=url;p.is_closed.return_value=False;p.frames=[p]
    return p


class NavigationResilience(unittest.TestCase):
    def test_report_can_lead_to_intermediate_post_link(self):
        p=page();ctx=Mock(pages=[p]);state=[0]
        post=page('https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery')
        def rows(_):
            return [row('報告書閲覧',0,True)] if state[0]==0 else [row('電子ポスト',1)]
        p.locator.return_value.evaluate_all.side_effect=rows
        def nth(i):
            loc=Mock()
            def click(**kw):
                if i==0:state[0]=1;p.url='https://site.sbisec.co.jp/intermediate'
                else:ctx.pages.append(post)
            loc.click.side_effect=click
            return loc
        p.locator.return_value.nth.side_effect=nth
        self.assertIs(open_post(p,ctx),post)

    def test_navigation_timeout_after_dispatch_still_observes_popup(self):
        p=page();ctx=Mock(pages=[p])
        post=page('https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery')
        p.locator.return_value.evaluate_all.return_value=[row('報告書閲覧',0,True)]
        def click(**kw):
            self.assertTrue(kw['no_wait_after'])
            ctx.pages.append(post)
            raise TimeoutError('navigation load did not finish')
        p.locator.return_value.nth.return_value.click.side_effect=click
        self.assertIs(open_post(p,ctx),post)

    def test_same_destination_header_duplicates_are_one_navigation(self):
        p=page();ctx=Mock(pages=[p])
        post=page('https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery')
        p.locator.return_value.evaluate_all.return_value=[row('口座管理',0,href='/same'),row('口座管理',1,href='https://site.sbisec.co.jp/same')]
        p.locator.return_value.nth.return_value.click.side_effect=lambda **kw:ctx.pages.append(post)
        self.assertIs(open_post(p,ctx),post)
        p.locator.return_value.nth.return_value.click.assert_called_once()

    def test_foreign_explicit_destination_is_not_followed(self):
        p=page();ctx=Mock(pages=[p])
        p.locator.return_value.evaluate_all.return_value=[row('電子ポスト',0,href='https://example.org/?secret=fixture')]
        with self.assertRaises(CredentialError):
            open_post(p,ctx,clock=Mock(side_effect=[0,0,31]))
        p.locator.return_value.nth.assert_not_called()

    def test_http_post_destination_is_not_followed(self):
        p=page();ctx=Mock(pages=[p])
        p.locator.return_value.evaluate_all.return_value=[row('電子ポスト',0,href='http://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery')]
        with self.assertRaises(CredentialError):
            open_post(p,ctx,clock=Mock(side_effect=[0,0,31]))
        p.locator.return_value.nth.assert_not_called()

    def test_multiple_post_tabs_fail_closed(self):
        p=page();a=page('https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery')
        b=page(a.url)
        with self.assertRaisesRegex(CredentialError,'複数'):
            open_post(p,Mock(pages=[p,a,b]))

    def test_timeout_diagnostic_does_not_include_url_tokens(self):
        p=page('https://site.sbisec.co.jp/private/fixture-token?session=fixture-secret')
        p.locator.return_value.evaluate_all.return_value=[]
        with self.assertRaises(CredentialError) as caught:
            open_post(p,Mock(pages=[p]),clock=Mock(side_effect=[0,0,31]))
        self.assertIn('site.sbisec.co.jp',str(caught.exception))
        self.assertNotIn('fixture-token',str(caught.exception))
        self.assertNotIn('fixture-secret',str(caught.exception))

    def test_closed_original_tab_does_not_interrupt_popup_wait(self):
        p=page();other=page();ctx=Mock(pages=[p,other]);p.is_closed.return_value=True
        other.locator.return_value.evaluate_all.return_value=[]
        with self.assertRaises(CredentialError):
            open_post(p,ctx,clock=Mock(side_effect=[0,0,31]))
        p.wait_for_timeout.assert_not_called()
        other.wait_for_timeout.assert_called_once()

    def test_cancel_after_first_poll_stops_without_reclick(self):
        p=page();p.locator.return_value.evaluate_all.return_value=[]
        with self.assertRaisesRegex(CredentialError,'中止'):
            open_post(p,Mock(pages=[p]),cancelled=Mock(side_effect=[False,True]))
        p.locator.return_value.nth.assert_not_called()


class NavigationBrowserFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:from playwright.sync_api import sync_playwright
        except ImportError:raise unittest.SkipTest('Playwright is not installed')
        cls.pw=sync_playwright().start()
        from browser_test_support import browser_executable
        executable=browser_executable(cls.pw)
        if not executable:
            cls.pw.stop()
            raise unittest.SkipTest('SBI_TEST_BROWSER, Edge or Chromium is needed for offline browser fixtures')
        cls.browser=cls.pw.chromium.launch(executable_path=executable,headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.context=self.browser.new_context()
        self.addCleanup(self.context.close)
        self.routes={};self.requests=[]
        def intercept(route):
            from urllib.parse import urlsplit
            path=urlsplit(route.request.url).path
            self.requests.append((path,route.request.method))
            body=self.routes.get(path,'<!doctype html><title>fixture</title>')
            if isinstance(body,dict):route.fulfill(**body)
            else:route.fulfill(body=body,content_type='text/html; charset=utf-8')
        self.context.route('**/*',intercept)
        self.page=self.context.new_page()

    def goto(self,path,html):
        self.routes[path]=html
        self.page.goto('https://site.sbisec.co.jp'+path)

    def test_real_image_input_and_stable_control_after_dom_insertion(self):
        self.goto('/fixture', '''<div>国内株式・投資信託
          <input type="image" alt="報告書閲覧" onclick="window.clicked='report';return false">
          </div><button disabled>報告書閲覧</button>''')
        locs=controls(self.page,['報告書閲覧'],True)
        self.assertEqual(len(locs),1)
        self.page.evaluate("document.body.insertAdjacentHTML('afterbegin','<button onclick=\"window.clicked=1\">別の操作</button>')")
        locs[0].click()
        self.assertEqual(self.page.evaluate('window.clicked'),'report')

    def test_multistep_popup_redirect_including_intermediate_post_link(self):
        self.routes['/menu']='''<table><tr><td>国内株式・投資信託</td><td>
          <input type="image" alt="報告書閲覧" onclick="location.href='/bridge';return false">
          </td></tr><tr><td>外国株式</td><td><a href="/wrong">報告書閲覧</a></td></tr></table>'''
        self.routes['/bridge']='''<a href="/sso" target="_blank">電子ポスト</a>'''
        self.routes['/sso']='''<script>setTimeout(()=>location.href='https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery',150)</script>'''
        self.goto('/start','''<a href="/account">口座管理</a><a href="/account">口座管理</a>
          <a href="/menu">取引報告書等（電子交付）</a>''')
        result=open_post(self.page,self.context,timeout=4)
        self.assertIn('user-delivery',result.url)
        self.assertNotIn(('/account','GET'),self.requests)
        self.assertNotIn(('/wrong','GET'),self.requests)
        self.assertIn(('/bridge','GET'),self.requests)

    def test_iframe_form_post_opens_electronic_post_popup(self):
        self.routes['/frame']='''<div>国内株式・投資信託<form action="/sso-post" method="post" target="_blank">
          <input type="image" alt="報告書閲覧"></form></div>'''
        self.routes['/sso-post']={'status':302,'headers':{'Location':'https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery'},'body':''}
        self.goto('/start','<iframe src="/frame"></iframe>')
        self.page.frame_locator('iframe').get_by_alt_text('報告書閲覧').wait_for()
        result=open_post(self.page,self.context,timeout=4)
        self.assertIn('user-delivery',result.url)
        self.assertIn(('/sso-post','POST'),self.requests)

    def test_aria_disabled_and_hidden_dom_controls_are_not_candidates(self):
        self.goto('/fixture','''<a href="/one" aria-disabled="true">電子ポスト</a>
          <div inert><a href="/two">電子ポスト</a></div>
          <a style="display:none" href="/three">電子ポスト</a>
          <a aria-labelledby="post-label" href="/ok"><span id="post-label">電子ポスト</span></a>''')
        candidates=select_rows(scan(self.page),['電子ポスト'])
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['href'],'/ok')


if __name__=='__main__':unittest.main()
