from wayforth import WayforthClient

client = WayforthClient()

# Check status
print(client.status())

# Search for inference services
results = client.search("fast cheap inference for coding")
for svc in results[:3]:
    print(f"{svc['name']} (tier {svc['coverage_tier']}) — {svc['endpoint_url']}")

# List all translation services
translators = client.list_services(category="translation")
print(f"Found {len(translators)} translation services")
