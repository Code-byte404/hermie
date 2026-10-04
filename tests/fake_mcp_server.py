"""A minimal stdio MCP server for tests (official mcp SDK). Shaped like analytics-mcp 0.7.0: list results are wrapped
as {"result": [...]} the way Google ADK serialises non-dict tool returns."""
import asyncio
import json
import os
import sys

from mcp.server.fastmcp import FastMCP

FIGURE = "SESSIONS 4242.17"
app = FastMCP("fake-analytics")


@app.tool()
def get_account_summaries() -> str:
    return json.dumps({"result": [
        {"account": "accounts/1", "display_name": "Main Co",
         "property_summaries": [{"property": "properties/111", "display_name": "Alpha Web"},
                                {"property": "properties/222", "display_name": "Alpha App"}]},
        {"account": "accounts/2", "display_name": "Side Co",
         "property_summaries": [{"property": "properties/333", "display_name": "Beta Site"}]}]})


@app.tool()
def run_report(property_id: str, date_ranges: list | None = None, dimensions: list | None = None,
               metrics: list | None = None) -> str:
    return json.dumps({"property": property_id, "rows": [{"country": "US", "sessions": FIGURE}],
                       "row_count": 1})


@app.tool()
async def slow_tool(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "done"


@app.tool()
def write_tool() -> str:
    return "this must never be reachable through Hermie"


@app.tool()
def fail_tool() -> str:
    raise RuntimeError("upstream API said no")


@app.tool()
def echo_env() -> str:
    return json.dumps({"env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid()})


@app.tool()
def exit_now() -> str:
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    app.run("stdio")
