const BACKOFF_MS = [1000, 2000, 4000];
const MAX_ATTEMPTS = 3;

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function fetchWithRetry(
  url: string,
  init: RequestInit,
  sleep: (ms: number) => Promise<void> = defaultSleep,
): Promise<Response> {
  let lastResponse: Response | undefined;
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    const response = await fetch(url, init);
    if (response.status < 500) {
      return response;
    }
    lastResponse = response;
    if (attempt < MAX_ATTEMPTS - 1) {
      await sleep(BACKOFF_MS[attempt]);
    }
  }
  return lastResponse!;
}
