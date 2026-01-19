import os
import logging

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from aidial_sdk import DIALApp
from aidial_sdk.chat_completion import ChatCompletion, Request, Response, Choice

from task.agents.web_search.web_search_agent import WebSearchAgent
from task.tools.base_tool import BaseTool
from task.tools.deployment.calculations_agent_tool import CalculationsAgentTool
from task.tools.deployment.content_management_agent_tool import ContentManagementAgentTool
from task.tools.mcp.mcp_client import MCPClient
from task.tools.mcp.mcp_tool import MCPTool
from task.utils.constants import DIAL_ENDPOINT, DEPLOYMENT_NAME

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_DDG_MCP_URL = os.getenv('DDG_MCP_URL', "http://localhost:8051/mcp")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all incoming requests"""
    
    async def dispatch(self, request: StarletteRequest, call_next):
        logger.info("=" * 80)
        logger.info("INCOMING REQUEST (Middleware)")
        logger.info(f"Method: {request.method}")
        logger.info(f"URL: {request.url}")
        logger.info(f"Path: {request.url.path}")
        logger.info(f"Query params: {dict(request.query_params)}")
        logger.info(f"Headers: {dict(request.headers)}")
        logger.info(f"Client: {request.client}")
        
        # Log registered routes if available
        if hasattr(request.app, 'routes'):
            logger.info(f"Registered routes: {[str(route.path) for route in request.app.routes]}")
        
        response = await call_next(request)
        logger.info(f"Response status: {response.status_code}")
        logger.info("=" * 80)
        return response


class WebSearchApplication(ChatCompletion):

    def __init__(self):
        super().__init__()
        logger.info("=" * 80)
        logger.info("Initializing WebSearchApplication")
        logger.info(f"DIAL_ENDPOINT: {DIAL_ENDPOINT}")
        logger.info(f"DEPLOYMENT_NAME: {DEPLOYMENT_NAME}")
        logger.info(f"DDG_MCP_URL: {_DDG_MCP_URL}")
        # Initialize tools
        logger.info("Initializing tools...")
        self.calculations_agent_tool = CalculationsAgentTool(endpoint=DIAL_ENDPOINT)
        self.content_management_agent_tool = ContentManagementAgentTool(endpoint=DIAL_ENDPOINT)
        self.mcp_tools: list[BaseTool] = []
        self._tools_initialized = False
        logger.info("WebSearchApplication initialized successfully")
        logger.info("=" * 80)

    async def _initialize_tools(self):
        """Initialize async MCP tools"""
        if not self._tools_initialized:
            logger.debug("Initializing async MCP tools...")
            mcp_client = await MCPClient.create(_DDG_MCP_URL)
            mcp_tool_models = await mcp_client.get_tools()
            self.mcp_tools = [
                MCPTool(client=mcp_client, mcp_tool_model=tool_model)
                for tool_model in mcp_tool_models
            ]
            self._tools_initialized = True
            logger.debug(f"MCP tools initialized: {[tool.name for tool in self.mcp_tools]}")

    async def chat_completion(self, request: Request, response: Response):
        logger.info("=" * 80)
        logger.info("WebSearchApplication.chat_completion called")
        logger.info(f"Request method: {getattr(request, 'method', 'N/A')}")
        logger.info(f"Request path: {getattr(request, 'url', {}).path if hasattr(request, 'url') else 'N/A'}")
        logger.info(f"Request headers: {dict(getattr(request, 'headers', {}))}")
        logger.info(f"Number of messages: {len(request.messages) if request.messages else 0}")
        if request.messages:
            for i, msg in enumerate(request.messages):
                logger.info(f"  Message {i}: role={getattr(msg, 'role', 'N/A')}, content_length={len(getattr(msg, 'content', '') or '')}")
        
        try:
            # Initialize async tools if not already done
            logger.debug("Initializing async tools if needed...")
            await self._initialize_tools()
            
            # Create tools list
            logger.debug("Creating tools list...")
            tools: list[BaseTool] = [
                *self.mcp_tools,
                self.calculations_agent_tool,
                self.content_management_agent_tool
            ]
            logger.info(f"Tools initialized: {[tool.name for tool in tools]}")
            
            # Create agent
            logger.debug("Creating WebSearchAgent...")
            agent = WebSearchAgent(tools=tools)
            
            # Create choice
            logger.debug("Creating response choice...")
            choice = response.create_choice()
            
            # Call agent
            logger.info(f"Calling agent.handle_request with deployment_name={DEPLOYMENT_NAME}")
            await agent.handle_request(
                deployment_name=DEPLOYMENT_NAME,
                choice=choice,
                request=request,
                response=response
            )
            logger.info("Agent.handle_request completed successfully")
        except Exception as e:
            logger.error(f"Error in chat_completion: {type(e).__name__}: {str(e)}", exc_info=True)
            raise
        finally:
            logger.info("=" * 80)


if __name__ == "__main__":
    logger.info("=" * 80)
    logger.info("Starting Web Search Agent Application")
    logger.info(f"Creating DIALApp with deployment_name='web-search-agent'")
    logger.info(f"Host: 0.0.0.0, Port: 5003")
    
    app_impl = WebSearchApplication()
    logger.info("WebSearchApplication instance created")
    
    # Create DIALApp first, then add chat completion using the method
    app = DIALApp()
    app.add_chat_completion(
        deployment_name="web-search-agent",
        impl=app_impl
    )
    logger.info("DIALApp created successfully")
    logger.info(f"Expected route: /openai/deployments/web-search-agent/chat/completions")
    
    # Add request logging middleware
    logger.info("Adding request logging middleware...")
    app.add_middleware(RequestLoggingMiddleware)
    
    # Log all registered routes
    if hasattr(app, 'routes'):
        logger.info("Registered routes:")
        for route in app.routes:
            logger.info(f"  - {route.path} (methods: {getattr(route, 'methods', 'N/A')})")
    else:
        logger.warning("Could not access app.routes - DIALApp might wrap the routes differently")
    
    # Try to access underlying FastAPI app
    if hasattr(app, 'app'):
        logger.info(f"DIALApp wraps another app: {type(app.app)}")
        if hasattr(app.app, 'routes'):
            logger.info("Underlying app routes:")
            for route in app.app.routes:
                logger.info(f"  - {route.path} (methods: {getattr(route, 'methods', 'N/A')})")
    
    logger.info("=" * 80)
    
    uvicorn.run(app, host="0.0.0.0", port=5003, log_level="debug")
