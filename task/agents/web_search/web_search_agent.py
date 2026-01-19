from task.agents.base_agent import BaseAgent
from task.agents.web_search._prompts import SYSTEM_PROMPT
from task.tools.base_tool import BaseTool
from task.utils.constants import DIAL_ENDPOINT


class WebSearchAgent(BaseAgent):

    def __init__(self, tools: list[BaseTool]):
        super().__init__(
            endpoint=DIAL_ENDPOINT,
            system_prompt=SYSTEM_PROMPT,
            tools=tools
        )
