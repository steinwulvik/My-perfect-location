"""Match named waters to documented permit coverage, not a fixed lake-ID whitelist."""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from html.parser import HTMLParser
from datetime import date
import time
import re
import ssl
import certifi

SOURCES = [
    {'url':'https://www.inatur.no/fiske/69c1b9391c4ee565bb51f02d/fiske-i-ostmarka-nasjonalpark-ofa',
     'center':(59.88,10.96), 'rangeKm':25, 'municipalities':['Oslo','Nordre Follo','Lørenskog','Rælingen','Enebakk'],
     # Only waters explicitly named on OFA's permit page, not all lakes in the area.
     'names':['Gjersrudtjern','Stensrudtjern','Skraperudtjern','Nøklevann','Ulsrudvann','Lutvann',
              'Hauktjern','Steinsjøen','Eriksvannet','Tonevann','Børtervann',
              'Fløyta','Fri-Elvåga','Geitsjøen','Grinderen','Mosjøen','Murterudvann','Mønevann',
              'Rausjøen','Rolandsjøen','Røyrivann','Sværsvann','Setertjern','Syverudtjern',
              'Tappenbergvann','Skålsjøen','Østensjøvannet','Biritjern','Grastjern']},
    {'url':'https://www.inatur.no/fiske/532e9d4fe4b0be7038a73f84/orretfiske-i-ralingsmarka',
     'center':(59.90,11.07), 'rangeKm':20, 'municipalities':['Rælingen'],
     'names':['Åmotdammen','Ramstadsjøen','Tristillen','Nessetertjerna','Nessetertjenn','Langtjenn','Fiskelausa','Merratjerna','Nordre Merratjern','Søndre Merratjern','Mørkåstjenn','Gryta','Myrdammen']},
    {'url':'https://www.inatur.no/fiske/50d2e4bce4b04b85c8956780/velkommen-til-fiskeopplevelser-i-blaker',
     'center':(59.90,11.29), 'rangeKm':15, 'municipalities':['Lillestrøm','Aurskog-Høland'],
     'names':['Øvre Helltjern','Nedre Helltjern','Igletjern']},
    {'url':'https://www.inatur.no/fiske/5cc81ca7c77ee50003c427e0',
     'center':(59.85,10.98), 'rangeKm':15, 'municipalities':['Lørenskog','Rælingen','Enebakk'],
     'names':['Mønevann','Fløyta','Geitsjøen','Røyrivann','Nordre Krokvann','Tappenbergvann','Tappenbergvannet','Midtre Krokvann','Søndre Krokvann','Grinderen','Endtjern','Malmeren','Nedre Sætertjern','Øvre Sætertjern','Østre Huktjern','Vestre Huktjern','Skålsjøen']}
]
ALIASES = {'Øvre Helltjern':['Ø.Helltjern'], 'Nedre Helltjern':['N.Helltjern'],
           'Tappenbergvann':['Tappenbergvannet'], 'Øvre Sætertjern':['Sætertjerna'],
           'Nessetertjenn':['Nessetertjerna'], 'Nordre Merratjern':['Merratjerna'],
           'Søndre Merratjern':['Merratjerna'],
           'Nøklevann':['Nøklevannet'], 'Ulsrudvann':['Ulsrudvannet'],
           'Lutvann':['Lutvannet'], 'Murterudvann':['Murterudvannet','Morterudvann','Morterudvannet'],
           'Mønevann':['Mønevannet'], 'Røyrivann':['Røyrivannet'],
           'Børtervann':['Børtevann'], 'Sværsvann':['Sværsvannet']}
CACHE = {}


class Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'): self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden: self.parts.append(data)


def source_text(url):
    cached=CACHE.get(url)
    if cached and time.time()-cached[0]<1800: return cached[1]
    req=Request(url,headers={'User-Agent':'MyPerfectLocation/0.1 (local learning project)'})
    with urlopen(req,timeout=20,context=ssl.create_default_context(cafile=certifi.where())) as response:
        parser=Text(); parser.feed(response.read().decode('utf-8'))
    text=re.sub(r'\s+',' ',' '.join(parser.parts)).casefold()
    if not ('fiskekort' in text and ('kjøp' in text or 'selger' in text)):
        raise RuntimeError('Fiskekorttilbudet kunne ikke bekreftes.')
    CACHE[url]=(time.time(),text)
    return text


def discover_registry(fetch, lat, lon, radius_km, point_params, nearest_point, distance, failures=None):
    sources=[s for s in SOURCES if distance(lat,lon,*s['center'])<=radius_km+s['rangeKm']]
    registry={}
    def lookup(source):
        text=source_text(source['url'])
        names=[n for n in source['names'] if any(a.casefold() in text for a in [n]+ALIASES.get(n,[]))]
        if not names: return []
        # NVE and the permit seller can use different, documented name variants.
        variants={variant for name in names for variant in [name]+ALIASES.get(name,[])}
        quoted=','.join("'"+n.replace("'","''")+"'" for n in sorted(variants))
        params=point_params(lat,lon,radius_km*1000,{'where':'navn IN ('+quoted+')','outFields':'navn,vatnlnr,kommune','resultRecordCount':1000})
        data=fetch('https://kart.nve.no/enterprise/rest/services/Innsjodatabase2/MapServer/5/query?'+urlencode(params),timeout=35)
        if data.get('error') or data.get('exceededTransferLimit'): raise RuntimeError('Fiskekortområdene kunne ikke stedfestes.')
        found=[]
        for f in data.get('features',[]):
            a=f.get('attributes',{}); point=nearest_point(*source['center'],[f.get('geometry') or {}])
            if a.get('navn') not in variants: continue
            if not point or distance(*source['center'],*point)>source['rangeKm']: continue
            municipalities=str(a.get('kommune') or '').casefold()
            if not any(m.casefold() in municipalities for m in source['municipalities']): continue
            found.append((str(a['vatnlnr']),{'name':a['navn'],'checkedOn':date.today().isoformat(),
                'drinkingWaterStatus':'unknown_assumed_not_drinking','fishPresenceSource':source['url'],
                'permitUrl':source['url'],'permitSource':source['url']}))
        return found
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs=[(s,pool.submit(lookup,s)) for s in sources]
        for source,job in jobs:
            try: registry.update(job.result())
            except Exception:
                if failures is not None: failures[source['url']]='Fiskekortkilden kunne ikke kontrolleres nå.'
    return registry
