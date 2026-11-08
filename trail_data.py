"""OSM walking/ski routes and paths; distance to the closest mapped segment."""
import math


def trail_query(lat, lon, radius):
    area = f'(around:{radius},{lat},{lon})'
    dlat = radius/110000
    dlon = dlat/max(.01, math.cos(math.radians(lat)))
    bounds = f'{lat-dlat},{lon-dlon},{lat+dlat},{lon+dlon}'
    return ('(nwr'+area+'["route"~"^(hiking|foot|ski)$"];'
            'way'+area+'["piste:type"="nordic"];'
            'way'+area+'["highway"~"^(path|footway)$"]["name"];'
            'way'+area+'["highway"~"^(path|footway)$"]["foot"="designated"];'
            ');out geom('+bounds+');')


def is_trail(tags):
    return (tags.get('route') in ('hiking', 'foot', 'ski') or tags.get('piste:type') == 'nordic'
            or tags.get('highway') in ('path', 'footway'))


def closest_point(item, lat, lon):
    # Local metric projection is sufficient for selecting a nearby map point.
    cos_lat = max(.001, math.cos(math.radians(lat)))
    def xy(p):
        return ((p['lon']-lon)*111320*cos_lat, (p['lat']-lat)*111320)
    geometries = [item.get('geometry', [])]
    geometries += [member.get('geometry', []) for member in item.get('members', [])]
    candidates = []
    for geometry in geometries:
        previous = None
        for point in geometry:
            if not isinstance(point, dict) or not all(isinstance(point.get(k), (float, int)) and math.isfinite(point[k]) for k in ('lat', 'lon')):
                previous = None
                continue
            x, y = xy(point)
            candidates.append((x, y))
            if previous is not None:
                a, b = previous
                dx, dy = x-a, y-b
                length = dx*dx + dy*dy
                t = max(0, min(1, -(a*dx+b*dy)/length)) if length else 0
                candidates.append((a+t*dx, b+t*dy))
            previous = (x, y)
    if not candidates:
        return None
    x, y = min(candidates, key=lambda p: p[0]**2+p[1]**2)
    return {'lat': lat+y/111320, 'lon': lon+x/(111320*cos_lat)}


def normalize_trails(elements, lat, lon):
    result = {}
    for item in elements:
        key = (item.get('type'), item.get('id'))
        if is_trail(item.get('tags', {})):
            point = closest_point(item, lat, lon)
            if point:
                item = {**item, 'nearbyPoint': point}
            # No route geometry or center must not become an invented location.
            item = {k: v for k, v in item.items() if k not in ('geometry', 'members')}
        result[key] = item
    return list(result.values())
