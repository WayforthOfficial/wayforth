# wayforth-langgraph

LangGraph drop-in adapter for Wayforth — one import, one init, execution via proxy.

## Install

```bash
pip install wayforth-langgraph
```

## Usage

```python
from wayforth_langgraph import WayforthTool
from langgraph.prebuilt import create_react_agent

wayforth = WayforthTool(api_key=os.environ["WAYFORTH_API_KEY"])
agent = create_react_agent(llm, tools=[wayforth])
```

Get an API key at [wayforth.io/signup](https://wayforth.io/signup).
