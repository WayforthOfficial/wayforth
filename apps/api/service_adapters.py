import asyncio
import httpx

SERVICE_CONFIGS = {
    "groq":        {"key_var": "GROQ_API_KEY",        "credits": 3},
    "deepl":       {"key_var": "DEEPL_API_KEY",       "credits": 1},
    "openweather": {"key_var": "OPENWEATHER_API_KEY", "credits": 1},
    "newsapi":     {"key_var": "NEWSAPI_API_KEY",     "credits": 1},
    "resend":      {"key_var": "RESEND_API_KEY",      "credits": 2},
    "serper":      {"key_var": "SERPER_API_KEY",      "credits": 1},
    "assemblyai":  {"key_var": "ASSEMBLYAI_API_KEY",  "credits": 5},
    "stability":   {"key_var": "STABILITY_API_KEY",   "credits": 10},
}


async def call_groq(params: dict, api_key: str) -> dict:
    model = params.get("model", "llama-3.3-70b-versatile")
    messages = params.get("messages", [])
    max_tokens = params.get("max_tokens", 1024)
    if not messages:
        raise Exception("params.messages is required")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
    if r.status_code != 200:
        raise Exception(f"Groq error {r.status_code}: {r.text[:200]}")
    data = r.json()
    choice = data["choices"][0]
    usage = data.get("usage", {})
    return {
        "content": choice["message"]["content"],
        "model": data.get("model", model),
        "tokens_used": usage.get("total_tokens", 0),
    }


async def call_deepl(params: dict, api_key: str) -> dict:
    text = params.get("text", "")
    target_lang = params.get("target_lang", "")
    if not text or not target_lang:
        raise Exception("params.text and params.target_lang are required")
    payload = {"text": [text], "target_lang": target_lang}
    if "source_lang" in params:
        payload["source_lang"] = params["source_lang"]
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api-free.deepl.com/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code != 200:
        raise Exception(f"DeepL error {r.status_code}: {r.text[:200]}")
    translation = r.json()["translations"][0]
    return {
        "translated_text": translation["text"],
        "detected_source_lang": translation.get("detected_source_language", ""),
    }


async def call_openweather(params: dict, api_key: str) -> dict:
    query_params: dict = {"appid": api_key, "units": "metric"}
    if "q" in params:
        query_params["q"] = params["q"]
    elif "lat" in params and "lon" in params:
        query_params["lat"] = params["lat"]
        query_params["lon"] = params["lon"]
    else:
        raise Exception("params.q (city name) or params.lat+lon are required")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params=query_params,
        )
    if r.status_code != 200:
        raise Exception(f"OpenWeather error {r.status_code}: {r.text[:200]}")
    data = r.json()
    temp_c = data["main"]["temp"]
    return {
        "city": data.get("name", ""),
        "temp_c": round(temp_c, 1),
        "temp_f": round(temp_c * 9 / 5 + 32, 1),
        "condition": data["weather"][0]["description"],
        "humidity": data["main"]["humidity"],
        "wind_kph": round(data["wind"]["speed"] * 3.6, 1),
    }


async def call_newsapi(params: dict, api_key: str) -> dict:
    q = params.get("q", "")
    if not q:
        raise Exception("params.q is required")
    page_size = min(int(params.get("page_size", 5)), 10)
    query_params = {
        "q": q,
        "language": params.get("language", "en"),
        "pageSize": page_size,
        "apiKey": api_key,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://newsapi.org/v2/everything", params=query_params)
    if r.status_code != 200:
        raise Exception(f"NewsAPI error {r.status_code}: {r.text[:200]}")
    data = r.json()
    articles = []
    for a in data.get("articles", [])[:page_size]:
        articles.append({
            "title": a.get("title", ""),
            "description": a.get("description", ""),
            "url": a.get("url", ""),
            "published_at": a.get("publishedAt", ""),
            "source": (a.get("source") or {}).get("name", ""),
        })
    return {"articles": articles}


async def call_resend(params: dict, api_key: str) -> dict:
    from_addr = params.get("from", "")
    to_addr = params.get("to", "")
    subject = params.get("subject", "")
    html = params.get("html", "")
    if not from_addr or not to_addr or not subject:
        raise Exception("params.from, params.to, and params.subject are required")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": to_addr, "subject": subject, "html": html or subject},
        )
    if r.status_code == 403:
        raise Exception(
            "Resend 403: The 'from' address must use a verified domain. "
            "Verify your domain at resend.com/domains before sending."
        )
    if r.status_code not in (200, 201):
        raise Exception(f"Resend error {r.status_code}: {r.text[:200]}")
    data = r.json()
    return {"email_id": data.get("id", ""), "status": "sent"}


async def call_serper(params: dict, api_key: str) -> dict:
    q = params.get("q", "")
    if not q:
        raise Exception("params.q is required")
    num = min(int(params.get("num", 5)), 10)
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": q, "num": num, "gl": params.get("gl", "us")},
        )
    if r.status_code != 200:
        raise Exception(f"Serper error {r.status_code}: {r.text[:200]}")
    data = r.json()
    organic = []
    for item in data.get("organic", [])[:num]:
        organic.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    result: dict = {"organic": organic}
    answer_box = data.get("answerBox", {})
    if answer_box:
        result["answer_box"] = answer_box.get("answer") or answer_box.get("snippet", "")
    return result


async def call_assemblyai(params: dict, api_key: str) -> dict:
    audio_url = params.get("audio_url", "")
    if not audio_url:
        raise Exception("params.audio_url is required")
    language_code = params.get("language_code", "en")
    headers = {"authorization": api_key, "content-type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.assemblyai.com/v2/transcript",
            headers=headers,
            json={
                "audio_url": audio_url,
                "language_code": language_code,
                "speech_models": params.get("speech_models", ["universal-2"]),
            },
        )
    if r.status_code != 200:
        raise Exception(f"AssemblyAI submit error {r.status_code}: {r.text[:200]}")
    job = r.json()
    transcript_id = job["id"]
    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"

    deadline = asyncio.get_event_loop().time() + 30
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            await asyncio.sleep(2)
            pr = await client.get(poll_url, headers=headers)
            if pr.status_code != 200:
                raise Exception(f"AssemblyAI poll error {pr.status_code}: {pr.text[:200]}")
            status = pr.json().get("status", "")
            if status == "completed":
                return {
                    "transcript_id": transcript_id,
                    "text": pr.json().get("text", ""),
                    "status": "completed",
                }
            if status == "error":
                raise Exception(f"AssemblyAI transcription failed: {pr.json().get('error','unknown')}")
            if asyncio.get_event_loop().time() >= deadline:
                return {
                    "status": "processing",
                    "transcript_id": transcript_id,
                    "poll_url": poll_url,
                }


async def call_stability(params: dict, api_key: str) -> dict:
    prompt = params.get("prompt", "")
    if not prompt:
        raise Exception("params.prompt is required")
    text_prompts = [{"text": prompt, "weight": 1.0}]
    negative_prompt = params.get("negative_prompt", "")
    if negative_prompt:
        text_prompts.append({"text": negative_prompt, "weight": -1.0})
    body = {
        "text_prompts": text_prompts,
        "width": int(params.get("width", 1024)),
        "height": int(params.get("height", 1024)),
        "steps": int(params.get("steps", 30)),
        "samples": 1,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
        )
    if r.status_code != 200:
        raise Exception(f"Stability AI error {r.status_code}: {r.text[:200]}")
    data = r.json()
    artifact = data["artifacts"][0]
    return {
        "image_base64": artifact["base64"],
        "seed": artifact.get("seed", 0),
        "finish_reason": artifact.get("finishReason", "SUCCESS"),
    }


ADAPTERS = {
    "groq":        call_groq,
    "deepl":       call_deepl,
    "openweather": call_openweather,
    "newsapi":     call_newsapi,
    "resend":      call_resend,
    "serper":      call_serper,
    "assemblyai":  call_assemblyai,
    "stability":   call_stability,
}
