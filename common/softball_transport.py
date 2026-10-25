"""Scoped capture/replay and request accounting for the existing HTTP transport."""
from contextlib import contextmanager
from contextvars import ContextVar

ACTIVE=ContextVar('softball_transport',default=None)
BUSY=ContextVar('softball_transport_busy',default=False)
STAGE=ContextVar('softball_transport_stage',default='unspecified')
PROVIDER=ContextVar('softball_transport_provider',default=None)


def operations():
    state=ACTIVE.get()
    return state['operations'] if state else []


@contextmanager
def source_transport(fetcher=None,*,offline=False):
    import requests
    session=requests.Session() if fetcher is not None and not offline else None
    state={'fetcher':fetcher,'offline':offline,'operations':[],'session':session}
    token=ACTIVE.set(state)
    try:yield state
    finally:
        ACTIVE.reset(token)
        if session is not None:session.close()


@contextmanager
def source_stage(name):
    token=STAGE.set(name)
    try:yield
    finally:STAGE.reset(token)


def intercept(url,kwargs):
    state=ACTIVE.get()
    if state is None or BUSY.get():return False,None
    fetcher=state['fetcher']
    if fetcher is None:
        if state['offline']:raise RuntimeError('Offline reprocessing cannot request a source')
        return False,None
    operation={'stage':STAGE.get(),'provider':PROVIDER.get(),'endpoint':url.split('?',1)[0]}
    before=getattr(fetcher,'requests',0)
    token=BUSY.set(True)
    try:
        if kwargs.get('session') is None:kwargs['session']=state.get('session')
        value=fetcher(url,**kwargs)
        operation.update(status='success',response_bytes=len(value.encode('utf-8')))
        operation['observed_at']=getattr(fetcher,'last_observed_at',None)
        return True,value
    except Exception as exc:
        operation.update(status='failed',error=type(exc).__name__)
        response=getattr(exc,'response',None)
        if response is not None:operation['http_status']=response.status_code
        raise
    finally:
        BUSY.reset(token)
        operation['network_attempts']=getattr(fetcher,'requests',0)-before
        state['operations'].append(operation)
