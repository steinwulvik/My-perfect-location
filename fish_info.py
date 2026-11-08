"""Short species introductions and attributed thumbnails from Wikipedia."""
import html
import re
from urllib.parse import urlencode, quote
from water_data import SPECIES, safe_url


def plain(value):
    return html.unescape(re.sub('<[^>]*>', '', value or '')).strip()


def wikipedia_fish(fetch, species):
    title = {'Lota lota': 'Lake (fisk)'}.get(species, SPECIES.get(species, species))
    params = {'action':'query', 'format':'json', 'formatversion':2,
              'prop':'extracts|pageimages', 'exintro':1, 'explaintext':1,
              'piprop':'thumbnail|name', 'pithumbsize':480, 'titles':title, 'redirects':1}
    data = fetch('https://no.wikipedia.org/w/api.php?' + urlencode(params), timeout=20)
    pages = data.get('query', {}).get('pages', [])
    page = pages[0] if pages else {}
    if page.get('missing') or not page.get('extract'):
        return {'available':False, 'message':'Ingen artsbeskrivelse tilgjengelig fra Wikipedia.'}
    text = page['extract'].strip()
    if len(text) > 550:
        end = text.rfind('. ', 100, 550)
        text = text[:end + 1] if end >= 100 else text[:550].rsplit(' ', 1)[0] + ' …'
    result = {'available':True, 'title':page['title'], 'description':text,
              'url':'https://no.wikipedia.org/wiki/' + quote(page['title'].replace(' ', '_')),
              'textLicenseUrl':'https://creativecommons.org/licenses/by-sa/4.0/', 'image':None}
    filename = page.get('pageimage')
    if filename:
        try:
            params = {'action':'query', 'format':'json', 'formatversion':2,
                      'prop':'imageinfo', 'iiprop':'url|extmetadata',
                      'iiurlwidth':480, 'titles':'File:' + filename}
            metadata = fetch('https://commons.wikimedia.org/w/api.php?' + urlencode(params), timeout=15)
            image_pages = metadata.get('query', {}).get('pages', [])
            info = image_pages[0].get('imageinfo', [{}])[0] if image_pages else {}
            ext = info.get('extmetadata', {})
            image_url = safe_url(info.get('thumburl'))
            if image_url:
                result['image'] = {'url':image_url, 'pageUrl':safe_url(info.get('descriptionurl')),
                                   'artist':plain(ext.get('Artist', {}).get('value')),
                                   'license':plain(ext.get('LicenseShortName', {}).get('value')),
                                   'licenseUrl':safe_url(ext.get('LicenseUrl', {}).get('value'))}
        except Exception:
            pass
    return result
