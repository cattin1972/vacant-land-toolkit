import requests

def count_buildings(lat, lon, radius_m=400):
    query = (
        '[out:json][timeout:25];'
        '(way["building"](around:%d,%f,%f);'
        'node["building"](around:%d,%f,%f);'
        ');'
        'out count;'
    ) % (radius_m, lat, lon, radius_m, lat, lon)
    r = requests.post("https://overpass-api.de/api/interpreter", data={"data": query}, timeout=30)
    print("status:", r.status_code)
    if r.status_code != 200:
        print(r.text[:500])
        return None
    return r.json()

print("Developed neighborhood (Austin, TX residential area):")
print(count_buildings(30.2900, -97.7400))

print()
print("Remote West Texas farmland:")
print(count_buildings(32.2500, -101.4800))
