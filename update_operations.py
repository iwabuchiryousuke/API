"""Application actions: per-account acquisition, all-account PDF application."""
from update_workflow import Workflow,Cancelled
from login_credentials import CredentialError


def collect_and_apply(base,month,playwright,authenticate,acquire,review,
                      progress=lambda **_:None,message=lambda _:None,cancelled=lambda:False):
    flow=Workflow(base,progress=progress,cancelled=cancelled)
    def collect(account,stage):
        if cancelled():raise Cancelled()
        progress(phase='AUTHORIZING',account=account)
        session=authenticate(base/'data',account,message)
        return acquire(playwright,account,stage,gmail_session=session,progress=message,
                       cancelled=cancelled,documents=True,capture_assets=True,target_dates=flow.target_dates)
    # A-D log in, while saved PDFs for A-E are always applied together.
    return flow.run(month,collect=collect,review=review)


def acquire_selected_pdf(base,account,playwright,authenticate,acquire,
                         message=lambda _:None,cancelled=lambda:False):
    if account not in ('A','B','C','D'):raise CredentialError('PDFの自動取得はA～Dです。')
    if cancelled():raise Cancelled()
    session=authenticate(base/'data',account,message)
    result=acquire(playwright,account,gmail_session=session,progress=message,
                   cancelled=cancelled,documents=True,capture_assets=False,target_dates=())
    if cancelled():raise Cancelled()
    return {'account':account,'saved':result['saved'],'duplicate':result['duplicate'],'applied':False}


def apply_saved(base,month,review,progress=lambda **_:None,cancelled=lambda:False):
    # Account selection controls where to fetch/copy PDFs, never this scope.
    return Workflow(base,progress=progress,cancelled=cancelled).run(month,review=review)
