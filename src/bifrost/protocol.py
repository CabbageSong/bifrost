import tomllib
from pathlib import Path

def load_config(path):
    with Path(path).open('rb') as f:
        return tomllib.load(f)

def http_request(request_id, method, path, headers=None, body=''):
    return {'type':'http_request','id':request_id,'method':method,'path':path or '/','headers':headers or {},'body':body}

def http_response(request_id, status, headers=None, body='', error=None):
    result={'type':'http_response','id':request_id,'status':status,'headers':headers or {},'body':body}
    if error: result['error']=error
    return result
