"""Offline updater tests. Synthetic data and credentials are never real accounts."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock,patch
import zipfile

import code_update as cu


def digest(value):return hashlib.sha256(value).hexdigest()


class CodeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.base=self.root/'app';self.base.mkdir()
        self.existing={
            'app.py':b"APP_LABEL = 'old app'\n",
            'version.py':b"APP_VERSION = '3.5.12'\n",
            'requirements_pdf.txt':b'pypdf==6.10.0\n',
            'data/ledger.sqlite3':b'synthetic existing ledger\x00\xff',
            'data/private/login_C.dat':b'encrypted opaque existing bytes\x00\x81',
            'data/private/gmail_C.secure':b'encrypted opaque gmail bytes\x13',
            'personal_setup.enc':b'original encrypted provision bytes',
            'output/family.xlsx':b'original workbook untouched',
            'my_notes.txt':b'user custom file',
        }
        for name,value in self.existing.items():
            p=self.base/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value)

    def zip(self,entries=None,manifest=None):
        values={'repo-main/app.py':b"APP_LABEL = 'new app'\n",'repo-main/version.py':b"APP_VERSION = '3.5.13'\n"}
        if entries:values.update(entries)
        if manifest:
            hashes={name.removeprefix('repo-main/'):digest(value) for name,value in values.items() if name.startswith('repo-main/') and cu._allowed(tuple(name.split('/')[1:]))}
            data=hashes if manifest=='配布ファイル_SHA256.json' else {'version':'3.5.13','files':hashes}
            values['repo-main/'+manifest]=json.dumps(data,ensure_ascii=False).encode('utf-8')
        archive=self.root/('source_'+str(len(list(self.root.glob('*.zip'))))+'.zip')
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
            for name,value in values.items():z.writestr(name,value)
        return archive

    def preserved(self):
        for name,value in self.existing.items():
            if name not in {'app.py','version.py','requirements_pdf.txt'}:
                self.assertEqual((self.base/name).read_bytes(),value,name)

    def test_prepare_has_no_mutations_and_reviews_private_file_count(self):
        archive=self.zip({'repo-main/data/private/login_C.dat':b'do not import','repo-main/personal_setup.enc':b'do not import','repo-main/other.csv':b'not code'})
        before={str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        plan=cu.prepare_update(self.base,archive)
        self.assertEqual((plan.current_version,plan.new_version),('3.5.12','3.5.13'))
        self.assertEqual(plan.ignored_private_count,2);self.assertEqual(plan.ignored_other_count,1)
        self.assertEqual(plan.code_file_count,2);self.assertFalse(plan.manifest_verified)
        after={str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_apply_backs_up_code_and_preserves_data_settings_excel(self):
        plan=cu.prepare_update(self.base,self.zip({'repo-main/new_module.py':b'VALUE=1\n'}))
        result=cu.apply_update(plan)
        self.assertEqual((self.base/'version.py').read_bytes(),b"APP_VERSION = '3.5.13'\n")
        self.assertEqual((result.backup_dir/'code/app.py').read_bytes(),self.existing['app.py'])
        self.assertEqual((result.backup_dir/'code/version.py').read_bytes(),self.existing['version.py'])
        self.assertTrue((result.backup_dir/'completed.json').is_file())
        backup=json.loads((result.backup_dir/'manifest.json').read_text('utf-8'))
        self.assertEqual(backup['files']['app.py']['previous_sha256'],digest(self.existing['app.py']))
        self.assertIsNone(backup['files']['new_module.py']['previous_sha256'])
        self.assertFalse((result.backup_dir/'data').exists());self.preserved()

    def test_combined_app_data_zip_never_reads_private_payload(self):
        archive=self.zip({'repo-main/data/ledger.sqlite3':b'wrong db','repo-main/Data/PRIVATE/login_C.DAT':b'wrong login','repo-main/personal_setup.enc':b'wrong key','repo-main/output/test.xlsx':b'wrong output'})
        real_read=zipfile.ZipFile.read
        def guarded(z,member,*args,**kwargs):
            name=member.filename if isinstance(member,zipfile.ZipInfo) else member
            if any(token in name.casefold() for token in ['ledger.sqlite3','login_c.dat','personal_setup.enc','test.xlsx']):
                raise AssertionError('private payload was read')
            return real_read(z,member,*args,**kwargs)
        with patch.object(zipfile.ZipFile,'read',guarded):
            plan=cu.prepare_update(self.base,archive)
        self.assertEqual(plan.ignored_private_count,4)
        cu.apply_update(plan);self.preserved()

    def test_same_version_refresh_and_repeated_apply_noop(self):
        first=cu.prepare_update(self.base,self.zip());cu.apply_update(first)
        refresh=cu.prepare_update(self.base,self.zip({'repo-main/new.py':b'VALUE=2\n'}))
        self.assertEqual(refresh.current_version,refresh.new_version)
        cu.apply_update(refresh)
        same=cu.prepare_update(self.base,refresh.archive)
        result=cu.apply_update(same)
        self.assertEqual(result.updated_files,());self.assertIsNone(result.backup_dir)

    def test_downgrade_is_rejected_without_mutation(self):
        archive=self.zip({'repo-main/version.py':b"APP_VERSION = '3.5.11'\n"})
        with self.assertRaisesRegex(cu.CodeUpdateError,'旧バージョン'):cu.prepare_update(self.base,archive)
        self.assertFalse((self.base/'backups').exists());self.preserved()

    def test_version_code_is_not_executed_and_is_rejected(self):
        marker=self.root/'must_not_exist'
        evil=f"APP_VERSION = '3.5.13'\nopen({str(marker)!r},'w').write('bad')\n".encode()
        with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,self.zip({'repo-main/version.py':evil}))
        self.assertFalse(marker.exists());self.assertFalse((self.base/'backups').exists())

    def test_version_expression_is_not_evaluated(self):
        with self.assertRaises(cu.CodeUpdateError):
            cu.prepare_update(self.base,self.zip({'repo-main/version.py':b"APP_VERSION = __import__('os').getcwd()\n"}))

    def test_ast_literal_base_version_fallback(self):
        (self.base/'version.py').unlink();(self.base/'app.py').write_text("APP_VERSION = '3.5.12'\nraise RuntimeError('do not execute')\n")
        self.assertEqual(cu.prepare_update(self.base,self.zip()).current_version,'3.5.12')

    def test_requirements_change_requires_setup_but_does_not_run_pip(self):
        archive=self.zip({'repo-main/requirements_pdf.txt':b'pypdf==6.10.1\n'})
        plan=cu.prepare_update(self.base,archive);self.assertTrue(plan.requirements_changed)
        result=cu.apply_update(plan);self.assertTrue(result.requirements_changed);self.preserved()

    def test_existing_code_changed_after_review_aborts(self):
        plan=cu.prepare_update(self.base,self.zip());(self.base/'app.py').write_bytes(b'user edited code')
        with self.assertRaisesRegex(cu.CodeUpdateError,'変わりました'):cu.apply_update(plan)
        self.assertEqual((self.base/'app.py').read_bytes(),b'user edited code');self.preserved()

    def test_plan_content_checksum_is_rechecked(self):
        plan=cu.prepare_update(self.base,self.zip())
        bad=replace(plan,files=(replace(plan.files[0],content=b'mutated'),)+plan.files[1:])
        with self.assertRaisesRegex(cu.CodeUpdateError,'チェックサム'):cu.apply_update(bad)
        self.assertEqual((self.base/'app.py').read_bytes(),self.existing['app.py']);self.preserved()

    def test_replace_failure_rolls_back_existing_and_new_code(self):
        archive=self.zip({'repo-main/aaa_new.py':b'NEW=1\n','repo-main/requirements_pdf.txt':b'pypdf==6.10.1\n'})
        plan=cu.prepare_update(self.base,archive);actual=cu.os.replace;calls=[0]
        def fail_third(src,dst):
            calls[0]+=1
            if calls[0]==3:raise PermissionError('injected failure')
            return actual(src,dst)
        with patch.object(cu.os,'replace',side_effect=fail_third):
            with self.assertRaisesRegex(cu.CodeUpdateError,'更新前に戻しました'):cu.apply_update(plan)
        self.assertFalse((self.base/'aaa_new.py').exists())
        for name in ['app.py','version.py','requirements_pdf.txt']:self.assertEqual((self.base/name).read_bytes(),self.existing[name])
        self.preserved()

    def test_backup_verification_failure_prevents_every_code_change(self):
        plan=cu.prepare_update(self.base,self.zip());real=cu.shutil.copy2
        def corrupt(src,dst,*args,**kwargs):
            result=real(src,dst,*args,**kwargs);Path(dst).write_bytes(b'corrupted backup');return result
        with patch.object(cu.shutil,'copy2',side_effect=corrupt):
            with self.assertRaisesRegex(cu.CodeUpdateError,'バックアップ'):cu.apply_update(plan)
        self.assertEqual((self.base/'app.py').read_bytes(),self.existing['app.py']);self.preserved()

    def test_japanese_checksum_manifest_and_structured_manifest_are_verified(self):
        for name in ['配布ファイル_SHA256.json','update_manifest.json']:
            with self.subTest(name=name):self.assertTrue(cu.prepare_update(self.base,self.zip(manifest=name)).manifest_verified)

    def test_checksum_mismatch_and_missing_coverage_rejected(self):
        for hashes in [{'app.py':'0'*64,'version.py':'0'*64},{'version.py':digest(b"APP_VERSION = '3.5.13'\n")}]:
            archive=self.zip({'repo-main/配布ファイル_SHA256.json':json.dumps(hashes).encode()})
            with self.assertRaisesRegex(cu.CodeUpdateError,'チェックサム'):cu.prepare_update(self.base,archive)

    def test_manifest_private_hash_entries_do_not_trigger_private_reads(self):
        values={'app.py':digest(b"APP_LABEL = 'new app'\n"),'version.py':digest(b"APP_VERSION = '3.5.13'\n"),'data/private/login_C.dat':'0'*64}
        archive=self.zip({'repo-main/配布ファイル_SHA256.json':json.dumps(values).encode(),'repo-main/data/private/login_C.dat':b'ignored'})
        self.assertTrue(cu.prepare_update(self.base,archive).manifest_verified)

    def test_traversal_absolute_windows_and_reserved_paths_rejected(self):
        for name in ['../outside.py','/outside.py','C:/outside.py','repo-main/../outside.py','repo-main\\outside.py','repo-main/app.py:secret','repo-main/con.py','repo-main/COM¹.py','repo-main/invalid?.py','repo-main/invalid*.py','repo-main/app.py.','repo-main/trailing /test.py']:
            with self.subTest(name=name):
                with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,self.zip({name:b'bad'}))
        self.assertFalse((self.root/'outside.py').exists());self.assertFalse((self.base/'backups').exists())

    def test_zip_symlink_and_special_entry_rejected(self):
        for mode in [stat.S_IFLNK|0o777,stat.S_IFIFO|0o600]:
            archive=self.zip()
            with zipfile.ZipFile(archive,'a') as z:
                info=zipfile.ZipInfo('repo-main/link.py');info.create_system=3;info.external_attr=mode<<16;z.writestr(info,b'../outside')
            with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,archive)

    def test_duplicate_casefold_paths_and_ambiguous_roots_rejected(self):
        for extra in [{'repo-main/APP.PY':b'duplicate'},{'second/app.py':b'APP=1\n','second/version.py':b"APP_VERSION='3.5.13'\n"}]:
            with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,self.zip(extra))

    def test_file_parent_collision_is_rejected(self):
        with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,self.zip({'repo-main/reference':b'file','repo-main/reference/excel_layout.json':b'{}'}))

    def test_huge_code_and_high_compression_are_rejected_before_mutation(self):
        with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,self.zip({'repo-main/huge.py':b'#'* (cu.MAX_CODE_BYTES+1)}))
        self.assertFalse((self.base/'backups').exists())

    def test_source_must_include_standard_app_and_version_files(self):
        archive=self.root/'no_app.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('lonely.py','VALUE=1')
        with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,archive)

    @unittest.skipUnless(hasattr(Path,'symlink_to'),'symlink support unavailable')
    def test_existing_reference_symlink_is_not_followed(self):
        outside=self.root/'outside';outside.mkdir()
        try:(self.base/'reference').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('symlink creation unavailable')
        with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,self.zip({'repo-main/reference/excel_layout.json':b'{}'}))
        self.assertFalse((outside/'excel_layout.json').exists())

    def test_unknown_user_json_and_docs_are_preserved(self):
        (self.base/'user_config.json').write_bytes(b'custom local config')
        archive=self.zip({'repo-main/user_config.json':b'bad config','repo-main/diagnostic_user.json':b'bad diagnostic'})
        plan=cu.prepare_update(self.base,archive);self.assertEqual(plan.ignored_other_count,2)
        cu.apply_update(plan)
        self.assertEqual((self.base/'user_config.json').read_bytes(),b'custom local config')
        self.assertFalse((self.base/'diagnostic_user.json').exists());self.preserved()

    def test_existing_excel_layout_requires_exact_byte_equality(self):
        reference=self.base/'reference';reference.mkdir()
        layout=reference/'excel_layout.json';layout.write_bytes(b'{"custom_layout":true}\n')
        with self.assertRaisesRegex(cu.CodeUpdateError,'Excel出力形式'):
            cu.prepare_update(self.base,self.zip({'repo-main/reference/excel_layout.json':b'{"custom_layout":false}\n'}))
        self.assertEqual(layout.read_bytes(),b'{"custom_layout":true}\n')
        self.assertFalse((self.base/'backups').exists())
        equal=cu.prepare_update(self.base,self.zip({'repo-main/reference/excel_layout.json':layout.read_bytes()}))
        self.assertNotIn('reference/excel_layout.json',[f.path for f in equal.files])

    def test_missing_excel_layout_can_be_installed(self):
        plan=cu.prepare_update(self.base,self.zip({'repo-main/reference/excel_layout.json':b'{"layout":1}\n'}))
        cu.apply_update(plan)
        self.assertEqual((self.base/'reference/excel_layout.json').read_bytes(),b'{"layout":1}\n')

    def test_keyboard_interrupt_during_replace_rolls_back(self):
        plan=cu.prepare_update(self.base,self.zip());actual=cu.os.replace;calls=[0]
        def interrupt_second(src,dst):
            calls[0]+=1
            if calls[0]==2:raise KeyboardInterrupt()
            return actual(src,dst)
        with patch.object(cu.os,'replace',side_effect=interrupt_second):
            with self.assertRaisesRegex(cu.CodeUpdateError,'更新前に戻しました'):cu.apply_update(plan)
        self.assertEqual((self.base/'app.py').read_bytes(),self.existing['app.py'])
        self.assertEqual((self.base/'version.py').read_bytes(),self.existing['version.py']);self.preserved()

    def test_protected_source_root_cannot_be_reinterpreted_as_code(self):
        archive=self.root/'protected_only.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('data/app.py',b'sensitive synthetic bytes')
            z.writestr('data/version.py',b"APP_VERSION='3.5.13'\n")
        with patch.object(zipfile.ZipFile,'read',side_effect=AssertionError('protected payload read')):
            with self.assertRaises(cu.CodeUpdateError):cu.prepare_update(self.base,archive)
        self.assertFalse((self.base/'backups').exists());self.preserved()

    def test_backup_code_roots_in_combined_zip_are_ignored_without_reads(self):
        archive=self.zip({'repo-main/backups/old/app.py':b'not source','repo-main/backups/old/version.py':b"APP_VERSION='9.9.9'\n"})
        real_read=zipfile.ZipFile.read
        def guarded(z,member,*args,**kwargs):
            name=member.filename if isinstance(member,zipfile.ZipInfo) else member
            if '/backups/' in name:raise AssertionError('backup payload read')
            return real_read(z,member,*args,**kwargs)
        with patch.object(zipfile.ZipFile,'read',guarded):plan=cu.prepare_update(self.base,archive)
        self.assertEqual(plan.ignored_private_count,2)
        self.assertEqual(plan.new_version,'3.5.13')

    def test_gui_declined_confirmation_does_not_apply(self):
        archive=self.zip();plan=cu.prepare_update(self.base,archive)
        with patch('tkinter.Tk',return_value=Mock()),patch('tkinter.filedialog.askopenfilename',return_value=str(archive)),patch('tkinter.messagebox.askyesno',return_value=False) as confirm,patch.object(cu,'prepare_update',return_value=plan),patch.object(cu,'apply_update') as apply:
            self.assertEqual(cu.main(),0)
        apply.assert_not_called();self.assertFalse((self.base/'backups').exists())
        self.assertEqual(confirm.call_args.kwargs['default'],'no')
        self.assertIn('終了',confirm.call_args.args[1]);self.assertIn('信頼できる',confirm.call_args.args[1])

    def test_gui_requires_review_and_reports_manual_dependency_setup(self):
        archive=self.zip({'repo-main/requirements_pdf.txt':b'pypdf==6.10.1\n'});plan=cu.prepare_update(self.base,archive)
        result=cu.UpdateResult(self.base/'backups/code_update_fixture','3.5.12','3.5.13',('app.py','version.py','requirements_pdf.txt'),True,False)
        with patch('tkinter.Tk',return_value=Mock()),patch('tkinter.filedialog.askopenfilename',return_value=str(archive)),patch('tkinter.messagebox.askyesno',return_value=True),patch('tkinter.messagebox.showinfo') as info,patch.object(cu,'prepare_update',return_value=plan),patch.object(cu,'apply_update',return_value=result) as apply:
            self.assertEqual(cu.main(),0)
        apply.assert_called_once_with(plan)
        self.assertIn('setup_all_windows.bat',info.call_args.args[1])
        self.assertFalse((self.base/'backups').exists())


if __name__=='__main__':unittest.main()
