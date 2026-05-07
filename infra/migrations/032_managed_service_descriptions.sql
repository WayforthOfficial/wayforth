-- Migration 032: update managed service descriptions for better keyword search coverage
-- Fixes: "read webpage" (jina), "stock price" (alphavantage), "news headlines" (newsapi),
--        "inference api" (groq), "text to speech" (elevenlabs), "weather forecast" (openweather)

UPDATE services SET description = 'Fast LLM inference API. Ultra-low latency Llama models. Best for real-time AI inference, chat completion, fast language model API calls.'
WHERE name = 'Groq';

UPDATE services SET description = 'Stock market data API. Real-time stock prices, financial data, equity data, market data, stock price lookup, historical prices.'
WHERE name = 'Alpha Vantage';

UPDATE services SET description = 'Read any webpage or URL. Web scraping, content extraction, URL reader, webpage text extraction, web content API.'
WHERE name = 'Jina Embeddings';

UPDATE services SET description = 'Weather data API. Current weather, weather forecast, temperature, weather conditions for any city or location.'
WHERE name = 'OpenWeatherMap';

UPDATE services SET description = 'Speech to text API. Audio transcription, voice to text, podcast transcription, meeting transcription, audio to text.'
WHERE name = 'AssemblyAI';

UPDATE services SET description = 'Send transactional email API. Email delivery, SMTP alternative, developer email API, email sending service.'
WHERE name = 'Resend API';

UPDATE services SET description = 'AI image generation API. Text to image, generate image, create image from text, stable diffusion, AI art generation.'
WHERE name = 'Stability AI API';

UPDATE services SET description = 'Text to speech API. TTS, voice synthesis, convert text to audio, realistic voice generation, speech synthesis API.'
WHERE name = 'ElevenLabs API';

UPDATE services SET description = 'News headlines API. Real-time news, latest news articles, breaking news, news feed from 80000+ sources worldwide.'
WHERE name = 'NewsAPI';
