#!/usr/bin/env python3
"""CMCC edition-only dispatch. Unknown editions preserve MediaDeck unchanged."""
import http.client,json,os,re,threading,urllib.parse,urllib.request,urllib.error,hashlib,hmac
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
_REG_LOCK=threading.Lock()
_REG_CACHE=None
_REG_IDENT=None
REG=Path(__file__).with_name('sources.json')
OPENLIST_PUBLIC_HOST='example.invalid'
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None
OPENER=urllib.request.build_opener(NoRedirect)
def reset_registry_cache():
 global _REG_CACHE,_REG_IDENT
 with _REG_LOCK:
  _REG_CACHE=None;_REG_IDENT=None
def _normalize_public_host(raw):
 host=(raw or '').strip().lower()
 if not host or any(c in host for c in '/:@?#\\') or host.startswith('.'):
  return 'example.invalid'
 if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host):
  return 'example.invalid'
 return host
def configure():
 """Load registry path and OpenList public host from the environment.

 Unset CMCC_OPENLIST_PUBLIC_HOST keeps example.invalid so resolve refuses
 rather than inferring a host from an untrusted registry URL.
 """
 global REG,OPENLIST_PUBLIC_HOST
 env_reg=(os.environ.get('CMCC_GATEWAY_REGISTRY') or '').strip()
 REG=Path(env_reg) if env_reg else Path(__file__).with_name('sources.json')
 OPENLIST_PUBLIC_HOST=_normalize_public_host(os.environ.get('CMCC_OPENLIST_PUBLIC_HOST') or '')
 reset_registry_cache()
configure()
def _registry_ident():
 st=REG.stat()
 return (st.st_ino,st.st_mtime_ns,st.st_size)
def load_registry():
 """Reuse parsed sources.json until inode/mtime/size change.

 Concurrent callers share one load. Any read/parse failure drops the cache so
 a previous registry cannot keep authorizing after the file is gone or corrupt.
 """
 global _REG_CACHE,_REG_IDENT
 with _REG_LOCK:
  try:
   ident=_registry_ident()
  except FileNotFoundError:
   _REG_CACHE=None;_REG_IDENT=None
   raise
  if _REG_CACHE is not None and _REG_IDENT==ident:
   return _REG_CACHE
  try:
   data=json.loads(REG.read_text())
  except Exception:
   _REG_CACHE=None;_REG_IDENT=None
   raise
  if not isinstance(data,dict):
   _REG_CACHE=None;_REG_IDENT=None
   raise ValueError('invalid registry')
  _REG_CACHE=data;_REG_IDENT=ident
  return _REG_CACHE
class Handler(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def send(self,code,body=b'',headers=None):
  self.send_response(code)
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.send_header('Content-Length',str(len(body)));self.end_headers()
  if self.command!='HEAD':self.wfile.write(body)
 def do_HEAD(self):self.do_GET()
 def do_GET(self):
  try:self.dispatch()
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception:self.send(502,b'Playback dispatch unavailable')
 def dispatch(self):
  if self.path=='/health':return self.send(200,b'ok')
  q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query); sid=(q.get('MediaSourceId') or q.get('mediaSourceId') or [''])[0]
  reg=load_registry()
  path=urllib.parse.urlsplit(self.path).path
  if path.startswith('/cmcc-source/'):
   name=path.removeprefix('/cmcc-source/');source=reg.get(name)
   cap=(q.get('cap') or [''])[0]
   if not source or not hmac.compare_digest(cap,hashlib.sha256(source['url'].encode()).hexdigest()):return self.send(403,b'Invalid source capability')
   return self.resolve(source)
  source=reg.get(sid)
  if not source:return self.forward()
  path=urllib.parse.urlsplit(self.path).path
  m=re.fullmatch(r'/(?:emby/)?[Vv]ideos/([^/]+)/(stream|original)(?:\.[A-Za-z0-9]+)?',path,re.I)
  if not m or (m[2].lower()=='stream' and (q.get('Static') or q.get('static') or [''])[0].lower() not in ('true','1')):return self.forward()
  item=m[1]
  if item not in source['item_ids']:return self.send(403,b'Edition not associated with item')
  token=self.headers.get('X-Emby-Token') or self.headers.get('X-MediaBrowser-Token')
  if not token:
   auth=self.headers.get('Authorization') or self.headers.get('X-Emby-Authorization') or ''
   match=re.search(r'token\s*=\s*"?([^",\s]+)',auth,re.I)
   token=match[1] if match else ''
  if not token:
   for k in ('api_key','ApiKey','apikey','X-Emby-Token'):
    if q.get(k):token=q[k][0];break
  if not token:return self.send(401,b'Authentication required')
  # Verify the caller's own token and edition visibility, never substitute admin credentials.
  url='http://127.0.0.1:8096/emby/Items/'+urllib.parse.quote(item,safe='')+'/PlaybackInfo'
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'X-Emby-Token':token}),timeout=90) as r:data=json.load(r)
  except urllib.error.HTTPError as e:return self.send(403 if e.code==403 else 401,b'Access denied')
  if not any(str(x.get('Id'))==sid for x in data.get('MediaSources',[])):return self.send(403,b'Edition unavailable')
  return self.resolve(source)
 def resolve(self,source):
  u=urllib.parse.urlsplit(source['url'])
  if u.scheme!='https' or u.netloc.lower()!=OPENLIST_PUBLIC_HOST or not u.path.startswith('/d/'):return self.send(502,b'Invalid source configuration')
  # Resolve through loopback OpenList, not its public management-domain WAF.
  local='http://127.0.0.1:5244'+u.path+'?'+u.query
  try:r=OPENER.open(urllib.request.Request(local,method='HEAD'),timeout=20)
  except urllib.error.HTTPError as e:r=e
  code=r.code;target=r.headers.get('Location','');r.close()
  dest=urllib.parse.urlsplit(target)
  if code!=302 or dest.scheme!='https' or not any((dest.hostname or '').endswith(s) for s in ('.cmecloud.cn','.139.com','.10086.cn')):return self.send(502,b'Cloud link unavailable')
  return self.send(302,headers={'Location':target,'Cache-Control':'private, no-store'})
 def forward(self):
  c=http.client.HTTPConnection('127.0.0.1',8300,timeout=60)
  try:
   headers={k:v for k,v in self.headers.items() if k.lower() not in ('connection','content-length','transfer-encoding')}
   c.request(self.command,self.path,headers=headers);r=c.getresponse();body=r.read(1024*1024)
   # Existing MediaDeck dispatch only returns redirects/errors, never video payloads.
   hs={k:v for k,v in r.getheaders() if k.lower() not in ('content-length','transfer-encoding','connection','server','date')}
   self.send(r.status,body,hs)
  finally:c.close()
if __name__=='__main__':ThreadingHTTPServer(('127.0.0.1',8339),Handler).serve_forever()
