"""Bounded, labelled OTP extraction. No arbitrary-number guessing or logging."""
import re,unicodedata,base64
from html.parser import HTMLParser
from email import policy
from email.parser import BytesParser

class VisibleHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag in ('br','p','div','tr','li','table'):self.parts.append('\n')
        if tag in ('td','th'):self.parts.append(' ')
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
        if tag in ('p','div','tr','li','table'):self.parts.append('\n')
        if tag in ('td','th'):self.parts.append(' ')
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)

def normalize(text):
    text=unicodedata.normalize('NFKC',text)
    return re.sub('[\u200b-\u200f\ufeff]','',text).replace('\r\n','\n').replace('\r','\n')

def visible_html(text):
    p=VisibleHTML();p.feed(text);return normalize(''.join(p.parts))

LABEL=re.compile(r'認\s*証\s*(?:コ\s*ー\s*ド|番\s*号)|ワンタイム(?:パスワード|コード)')
# Allow bounded explanatory text, not unrelated later numbers or links.
EXPLANATION=re.compile(r'^(?:(?:は|が|を|の|です|になります|となります|以下|下記|こちら|次|お客様|お客さま|とおり|通り|次の通り|次のとおり)|[\s:：、,=\[\]【】「」()<>。・―ー-])*$')

def candidates(text,length=None):
    text=re.sub(r'\s+',' ',normalize(text)).strip();found=set()
    lengths={length} if isinstance(length,int) and 4<=length<=12 else {6}
    for label in LABEL.finditer(text):
        tail=text[label.end():label.end()+100]
        # Stop at another label, a URL, or an email; never scan arbitrary later content.
        nextlabel=LABEL.search(tail)
        if nextlabel:tail=tail[:nextlabel.start()]
        tail=re.split(r'https?://|[\w.+-]+@',tail)[0]
        for match in re.finditer(r'(?<![A-Za-z0-9])([A-Za-z0-9]{4,12})(?![A-Za-z0-9])',tail):
            token=match.group(1);prefix=tail[:match.start()]
            if len(token) not in lengths or (not any(c.isdigit() for c in token) and (length is None or not token.isupper())):continue
            if not EXPLANATION.fullmatch(prefix):continue
            found.add(token)
    return found

def choose(text,length=None):
    codes=candidates(text,length)
    return next(iter(codes)) if len(codes)==1 else None

def raw_payload(encoded):
    """Convert Gmail RAW MIME to normal payload in memory, exclude attachments."""
    raw=base64.urlsafe_b64decode(encoded+'===')
    if len(raw)>2_000_000:raise ValueError('mail too large')
    msg=BytesParser(policy=policy.default).parsebytes(raw)
    def part(node):
        value={'mimeType':node.get_content_type(),'headers':[{'name':k,'value':str(v)} for k,v in node.items()]}
        if node.get_content_disposition()=='attachment':return value
        if node.is_multipart():value['parts']=[part(x) for x in node.iter_parts()]
        elif node.get_content_type() in ('text/plain','text/html'):
            decoded=node.get_payload(decode=True) or b''
            try:text=decoded.decode(node.get_content_charset() or 'utf-8')
            except (LookupError,UnicodeError):text=decoded.decode('utf-8',errors='replace')
            value['headers']=[h for h in value['headers'] if h['name'].lower()!='content-type']+[{'name':'Content-Type','value':node.get_content_type()+'; charset=utf-8'}]
            value['body']={'data':base64.urlsafe_b64encode(text.encode()).decode()}
        return value
    return part(msg)

def features(text,length=None):
    """Only structural counts; no code, text, identifiers or message headers."""
    text=normalize(text)
    return {'body_present':bool(text.strip()),'labels':len(LABEL.findall(text)),'candidates':len(candidates(text,length))}
