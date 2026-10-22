"""Fixed PDF identities. Login secrets live in DPAPI, never this module."""
OWNERS=dict(zip('ABCDE',('岩渕俊吾','岩渕将太','岩渕亮介','岩渕雅美','岩渕慶太')))
MAILBOXES=dict(zip('ABCD',('zz20021014@gmail.com','gg20021014@gmail.com','iriwabuchiryousuke@gmail.com','tousinoyobikou@gmail.com')))
def configure(store):
    # Defaults are for new accounts only. A saved PDF identity is user data.
    changes=[a for a in store.accounts() if a['id'] in OWNERS and not str(a.get('owner_name') or '').strip()]
    if changes:
        store.backup()
        with store.db:
            for a in changes:store.db.execute('UPDATE accounts SET owner_name=? WHERE id=?',(OWNERS[a['id']],a['id']))
