import { WayforthClient } from "../src";

async function main() {
  const client = new WayforthClient();

  console.log("=== Status ===");
  console.log(await client.status());

  console.log("\n=== Search: translate to Spanish ===");
  const results = await client.search("translate english to spanish", { limit: 3 });
  results.results.forEach(r =>
    console.log(`${r.name} (${r.score}) — ${r.endpoint_url}`)
  );

  console.log("\n=== List inference services ===");
  const inference = await client.listServices({ category: "inference", limit: 5 });
  console.log(`Found ${inference.length} inference services`);
  inference.slice(0, 3).forEach(s => console.log(` - ${s.name}`));
}

main().catch(console.error);
