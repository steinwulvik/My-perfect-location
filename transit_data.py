"""Nearby stop places from Entur Journey Planner, not OSM bus-stop guesses."""
def load_transit_stops(fetch, lat, lon, radius):
    query = ('{nearest(latitude:'+str(lat)+',longitude:'+str(lon)+',maximumDistance:'+str(radius)+
             ',maximumResults:1000,filterByPlaceTypes:[stopPlace])'
             '{edges{node{place{... on StopPlace{id name latitude longitude}}}}}}')
    data = fetch('https://api.entur.io/journey-planner/v3/graphql', method='POST',
                 data={'query': query}, headers={'ET-Client-Name': 'myperfectlocation-localapp'}, timeout=25)
    if data.get('errors'):
        raise RuntimeError('Holdeplasser kunne ikke hentes fra Entur')
    nearest = (data.get('data') or {}).get('nearest')
    if not isinstance(nearest, dict) or not isinstance(nearest.get('edges'), list):
        raise RuntimeError('Ufullstendig holdeplassoppslag fra Entur')
    stops = {}
    for edge in nearest['edges']:
        row = ((edge or {}).get('node') or {}).get('place') or {}
        if row.get('id') and row.get('latitude') is not None and row.get('longitude') is not None:
            stops[row['id']] = {'type': 'node', 'id': row['id'], 'lat': row['latitude'], 'lon': row['longitude'],
                                'kind': 'transit', 'source': 'Entur', 'tags': {'name': row.get('name') or 'Holdeplass'}}
    return list(stops.values())
