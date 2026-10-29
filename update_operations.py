"""Application actions: per-account acquisition, all-account PDF application."""
from update_workflow import Workflow,Cancelled
from login_credentials import CredentialError


def collect_and_apply(base,month,playwright,authenticate,acquire,review,
                      progress=lambda **_:None,message=lambda _:None,cancelled=lambda:False,current_only=False,reference_refresh=None):
    flow=Workflow(base,progress=progress,cancelled=cancelled)
    def collect(account,stage):
        if cancelled():raise Cancelled()
        progress(phase='AUTHORIZING',account=account)
        session=authenticate(base/'data',account,message)
        options=dict(gmail_session=session,progress=message,cancelled=cancelled,
                     documents=True,capture_assets=True,target_dates=flow.target_dates)
        if current_only:options['current_only']=True
        return acquire(playwright,account,stage,**options)
    # A-D log in, while saved PDFs for A-E are always applied together.
    return flow.run(month,collect=collect,review=review,current_only=current_only,reference_refresh=reference_refresh)


def acquire_selected_pdf(base,account,playwright,authenticate,acquire,
                         message=lambda _:None,cancelled=lambda:False):
    if account not in ('A','B','C','D'):raise CredentialError('PDFの自動取得はA～Dです。')
    if cancelled():raise Cancelled()
    session=authenticate(base/'data',account,message)
    result=acquire(playwright,account,gmail_session=session,progress=message,
                   cancelled=cancelled,documents=True,capture_assets=False,target_dates=())
    if cancelled():raise Cancelled()
    return {'account':account,'saved':result['saved'],'duplicate':result['duplicate'],'applied':False}


def apply_saved(base,month,review,progress=lambda **_:None,cancelled=lambda:False,current_only=False):
    # Account selection controls where to fetch/copy PDFs, never this scope.
    return Workflow(base,progress=progress,cancelled=cancelled).run(month,review=review,current_only=current_only)
