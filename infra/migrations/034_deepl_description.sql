-- 034: Enrich DeepL description with translation keywords so the ranker
-- correctly scores it above inference services for translation intents.

UPDATE services
SET description = 'DeepL Translation API. Translate text between languages. Supports Spanish, French, German, Italian, Japanese, Portuguese, Chinese, and 30+ languages. Best-in-class neural machine translation.'
WHERE slug = 'deepl';
