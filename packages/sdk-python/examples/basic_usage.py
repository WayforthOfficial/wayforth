import os

from wayforth import Wayforth

client = Wayforth(api_key=os.environ["WAYFORTH_API_KEY"])

# Check gateway health
print(client.status())

# Search for inference services (returns {"query", "results": [...]})
hits = client.search("fast cheap inference for coding")
for svc in hits["results"][:3]:
    print(f"{svc['name']} (tier {svc['coverage_tier']}) — WRI {svc.get('wri')}")

# List translation services from the catalog
translators = client.services(category="translation")
print(f"Found {len(translators)} translation services")

# Execute a managed service directly — no upstream key needed
print(client.execute("deepl", text="Hello world", target_lang="ES"))
