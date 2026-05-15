-- Migration 036: Reclassify MCP search wrappers from "agents" to "search"
-- MCP Tavily, Brave, and Exa are search services surfaced via MCP protocol;
-- categorising them as "agents" caused them to bleed into non-search result sets.

UPDATE services
SET category = 'search'
WHERE category = 'agents'
  AND (
    name ILIKE '%tavily%'
    OR name ILIKE '%brave%'
    OR name ILIKE '%exa%'
  );
