"""Road/path routing from Valhalla; Norwegian transit itineraries from Entur."""
import json
from urllib.parse import urlencode


def decode_polyline(encoded, precision=6):
    result, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        values = []
        for _ in range(2):
            value = shift = 0
            while True:
                if index >= len(encoded) or shift > 35:
                    raise ValueError('Ugyldig rutegeometri')
                byte = ord(encoded[index])-63
                index += 1
                if not 0 <= byte <= 63:
                    raise ValueError('Ugyldig rutegeometri')
                value |= (byte & 31) << shift
                shift += 5
                if byte < 32:
                    break
            values.append(~(value >> 1) if value & 1 else value >> 1)
        lat += values[0]
        lon += values[1]
        result.append([lat/(10**precision), lon/(10**precision)])
    return result


def load_route(fetch, lat, lon, to_lat, to_lon, mode):
    if mode not in ('walking', 'bicycling', 'driving', 'transit'):
        raise ValueError('Ukjent transportmetode')
    if mode == 'transit':
        query = ('{trip(from:{coordinates:{latitude:'+str(lat)+',longitude:'+str(lon)+'}},'
                 'to:{coordinates:{latitude:'+str(to_lat)+',longitude:'+str(to_lon)+'}},numTripPatterns:3)'
                 '{tripPatterns{duration legs{mode distance expectedStartTime expectedEndTime '
                 'line{publicCode name} fromPlace{name} toPlace{name} pointsOnLink{points}}}}}')
        data = fetch('https://api.entur.io/journey-planner/v3/graphql', method='POST',
                     data={'query': query}, headers={'ET-Client-Name': 'myperfectlocation-localapp'}, timeout=35)
        if data.get('errors'):
            raise RuntimeError('Kollektivruten kunne ikke hentes fra Entur')
        patterns = ((data.get('data') or {}).get('trip') or {}).get('tripPatterns') or []
        # A walk-only itinerary is not a public transport connection.
        patterns = [p for p in patterns if any(leg.get('mode') not in ('foot', 'walk') for leg in p.get('legs', []))]
        if not patterns:
            raise RuntimeError('Ingen kollektivforbindelse funnet for dette tidspunktet')
        pattern = patterns[0]
        lines, steps, meters = [], [], 0
        for leg in pattern['legs']:
            shape = (leg.get('pointsOnLink') or {}).get('points')
            if not shape:
                raise RuntimeError('Entur mangler rutegeometri')
            lines.append(decode_polyline(shape, 5))
            line = leg.get('line') or {}
            label = 'Gå' if leg['mode'] in ('foot', 'walk') else line.get('publicCode') or line.get('name') or leg['mode']
            steps.append({'text': f"{label}: {(leg.get('fromPlace') or {}).get('name', '')} → {(leg.get('toPlace') or {}).get('name', '')}",
                          'startTime': leg.get('expectedStartTime'), 'endTime': leg.get('expectedEndTime')})
            meters += leg.get('distance') or 0
        return {'lines': lines, 'seconds': pattern['duration'], 'distanceKm': meters/1000,
                'steps': steps, 'source': 'Entur · © OpenStreetMap'}
    request = {'locations': [{'lat': lat, 'lon': lon}, {'lat': to_lat, 'lon': to_lon}],
               'costing': {'walking': 'pedestrian', 'bicycling': 'bicycle', 'driving': 'auto'}[mode],
               'units': 'kilometers', 'language': 'nb-NO'}
    # Send an actual JSON body. Some routing proxies do not decode '+' spaces
    # in the GET json parameter and reject otherwise valid requests with code 100.
    data = fetch('https://valhalla1.openstreetmap.de/route', method='POST', data=request,
                 headers={'Content-Type': 'application/json'}, timeout=35)
    trip = data.get('trip') or {}
    if data.get('error') or trip.get('status') != 0 or not trip.get('legs'):
        raise RuntimeError('Ingen rute funnet for valgt transportmetode')
    lines = [decode_polyline(leg['shape']) for leg in trip['legs']]
    if any(len(line) < 2 for line in lines):
        raise RuntimeError('Ruten mangler kartgeometri')
    return {'lines': lines, 'seconds': trip['summary']['time'], 'distanceKm': trip['summary']['length'],
            'steps': [{'text': maneuver.get('instruction', '')} for leg in trip['legs'] for maneuver in leg.get('maneuvers', [])],
            'source': 'Valhalla/FOSSGIS · © OpenStreetMap'}
