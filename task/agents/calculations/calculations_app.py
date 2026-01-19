import os
import logging

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse
from aidial_sdk import DIALApp
from aidial_sdk.chat_completion import ChatCompletion, Request, Response, Choice

from task.agents.calculations.calculations_agent import CalculationsAgent
from task.agents.calculations.tools.simple_calculator_tool import SimpleCalculatorTool
from task.tools.base_tool import BaseTool
from task.agents.calculations.tools.py_interpreter.python_code_interpreter_tool import PythonCodeInterpreterTool
from task.tools.deployment.content_management_agent_tool import ContentManagementAgentTool
from task.tools.deployment.web_search_agent_tool import WebSearchAgentTool
from task.utils.constants import DIAL_ENDPOINT, DEPLOYMENT_NAME

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_PYTHON_MCP_URL = os.getenv('PYTHON_MCP_URL', "http://localhost:8050/mcp")


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
        
        # Log registered routes if available - try multiple ways
        app_to_check = request.app
        logger.info(f"Request app type: {type(app_to_check)}")
        logger.info(f"Request app class: {app_to_check.__class__.__name__}")
        
        # Try to get underlying app
        if hasattr(app_to_check, 'app'):
            logger.info(f"App wraps: {type(app_to_check.app)}")
            app_to_check = app_to_check.app
        
        # Try to get router
        if hasattr(app_to_check, 'router'):
            logger.info(f"Router type: {type(app_to_check.router)}")
            if hasattr(app_to_check.router, 'routes'):
                logger.info("Router routes:")
                for route in app_to_check.router.routes:
                    path = getattr(route, 'path', 'N/A')
                    path_regex = getattr(route, 'path_regex', 'N/A')
                    methods = getattr(route, 'methods', 'N/A')
                    logger.info(f"  - {path} (regex: {path_regex}, methods: {methods}, type: {type(route).__name__})")
        
        # Also check app.routes
        if hasattr(app_to_check, 'routes'):
            logger.info(f"App routes: {[str(route.path) for route in app_to_check.routes]}")
            logger.info("Detailed app routes:")
            for route in app_to_check.routes:
                path = getattr(route, 'path', 'N/A')
                path_regex = getattr(route, 'path_regex', 'N/A')
                methods = getattr(route, 'methods', 'N/A')
                logger.info(f"  - {path} (regex: {path_regex}, methods: {methods}, type: {type(route).__name__})")
        
        response = await call_next(request)
        logger.info(f"Response status: {response.status_code}")
        logger.info("=" * 80)
        return response


class CalculationsApplication(ChatCompletion):

    def __init__(self):
        super().__init__()
        logger.info("=" * 80)
        logger.info("Initializing CalculationsApplication")
        logger.info(f"DIAL_ENDPOINT: {DIAL_ENDPOINT}")
        logger.info(f"DEPLOYMENT_NAME: {DEPLOYMENT_NAME}")
        logger.info(f"PYTHON_MCP_URL: {_PYTHON_MCP_URL}")
        # Initialize tools
        logger.info("Initializing tools...")
        self.simple_calculator_tool = SimpleCalculatorTool()
        self.python_code_interpreter_tool = None  # Will be initialized asynchronously
        self.content_management_agent_tool = ContentManagementAgentTool(endpoint=DIAL_ENDPOINT)
        self.web_search_agent_tool = WebSearchAgentTool(endpoint=DIAL_ENDPOINT)
        self._tools_initialized = False
        logger.info("CalculationsApplication initialized successfully")
        logger.info("=" * 80)

    async def _initialize_tools(self):
        """Initialize async tools"""
        if not self._tools_initialized:
            logger.debug("Initializing async Python code interpreter tool...")
            self.python_code_interpreter_tool = await PythonCodeInterpreterTool.create(
                mcp_url=_PYTHON_MCP_URL,
                tool_name="execute_code",
                dial_endpoint=DIAL_ENDPOINT
            )
            self._tools_initialized = True
            logger.debug("Python code interpreter tool initialized")

    async def chat_completion(self, request: Request, response: Response):
        logger.info("=" * 80)
        logger.info("CalculationsApplication.chat_completion called")
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
            
            # Create tools list - filter out None values
            logger.debug("Creating tools list...")
            tools: list[BaseTool] = [
                tool for tool in [
                    self.simple_calculator_tool,
                    self.python_code_interpreter_tool,
                    self.content_management_agent_tool,
                    self.web_search_agent_tool
                ] if tool is not None
            ]
            logger.info(f"Tools initialized: {[tool.name for tool in tools]}")
            if len(tools) < 3:
                logger.warning(f"Some tools are missing! Expected at least 3 tools, got {len(tools)}")
            
            # Create agent
            logger.debug("Creating CalculationsAgent...")
            agent = CalculationsAgent(tools=tools)
            
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
    logger.info("Starting Calculations Agent Application")
    logger.info(f"Creating DIALApp with deployment_name='calculations-agent'")
    logger.info(f"Host: 0.0.0.0, Port: 5001")
    
    app_impl = CalculationsApplication()
    logger.info("CalculationsApplication instance created")
    logger.info(f"App impl type: {type(app_impl)}")
    logger.info(f"App impl class: {app_impl.__class__.__name__}")
    logger.info(f"Is ChatCompletion? {isinstance(app_impl, ChatCompletion)}")
    
    # Check if app_impl has chat_completion method
    if hasattr(app_impl, 'chat_completion'):
        logger.info("App impl has chat_completion method")
        import inspect
        sig = inspect.signature(app_impl.chat_completion)
        logger.info(f"chat_completion signature: {sig}")
    else:
        logger.error("App impl does NOT have chat_completion method!")
    
    # CRITICAL DEBUG: Print everything about app_impl before creating DIALApp
    print("=" * 80)
    print("BEFORE DIALApp CREATION - App Impl Details")
    print("=" * 80)
    print(f"App impl type: {type(app_impl)}")
    print(f"App impl class: {app_impl.__class__.__name__}")
    print(f"App impl MRO: {app_impl.__class__.__mro__}")
    print(f"Is ChatCompletion? {isinstance(app_impl, ChatCompletion)}")
    print(f"Has chat_completion? {hasattr(app_impl, 'chat_completion')}")
    if hasattr(app_impl, 'chat_completion'):
        import inspect
        sig = inspect.signature(app_impl.chat_completion)
        print(f"chat_completion signature: {sig}")
    print("=" * 80)
    
    # Create DIALApp first, then add chat completion using the method
    app = DIALApp()
    logger.info("DIALApp created (empty)")
    
    app.add_chat_completion(
        deployment_name="calculations-agent",
        impl=app_impl
    )
    logger.info("Chat completion route added via add_chat_completion()")
    
    # CRITICAL DEBUG: Print everything about DIALApp after adding chat completion
    print("=" * 80)
    print("AFTER DIALApp CREATION - DIALApp Details")
    print("=" * 80)
    print(f"DIALApp type: {type(app)}")
    print(f"DIALApp class: {app.__class__.__name__}")
    
    # Check ALL attributes (including private ones) to find where impl/deployment_name might be stored
    print("\nALL DIALApp attributes (including private):")
    all_attrs = dir(app)
    impl_attrs = [attr for attr in all_attrs if 'impl' in attr.lower() or 'deployment' in attr.lower() or 'chat' in attr.lower() or 'completion' in attr.lower()]
    print(f"Relevant attributes: {impl_attrs}")
    for attr in impl_attrs:
        try:
            value = getattr(app, attr)
            print(f"  {attr}: {type(value)} = {value}")
        except Exception as e:
            print(f"  {attr}: Error accessing - {e}")
    
    # Check __dict__ if available
    if hasattr(app, '__dict__'):
        print(f"\nDIALApp.__dict__ keys: {list(app.__dict__.keys())}")
        for key, value in app.__dict__.items():
            if 'impl' in key.lower() or 'deployment' in key.lower():
                print(f"  {key}: {type(value)} = {value}")
    
    print(f"\nNumber of routes AFTER add_chat_completion: {len(app.routes)}")
    print("All routes AFTER add_chat_completion:")
    for i, route in enumerate(app.routes):
        route_type = type(route).__name__
        path = getattr(route, 'path', 'N/A')
        methods = getattr(route, 'methods', 'N/A')
        print(f"  {i}. {route_type}: {path} (methods: {methods})")
    print("=" * 80)
    
    logger.info("DIALApp created and chat completion added successfully")
    logger.info(f"Expected route: /openai/deployments/calculations-agent/chat/completions")
    
    # Verify the route was registered
    expected_path = "/openai/deployments/calculations-agent/chat/completions"
    route_found = False
    for route in app.routes:
        route_path = getattr(route, 'path', '')
        if expected_path in route_path or route_path == expected_path:
            route_found = True
            logger.info(f"✓ Route found: {route_path} (methods: {getattr(route, 'methods', 'N/A')})")
            break
    if not route_found:
        logger.error(f"✗ Route NOT found: {expected_path}")
        logger.error("This is the problem! The route was not registered.")
    
    # CRITICAL: Check if DIALApp has methods to register routes or access impl
    print("=" * 80)
    print("CHECKING DIALApp METHODS")
    print("=" * 80)
    all_methods = [attr for attr in dir(app) if callable(getattr(app, attr, None)) and not attr.startswith('__')]
    relevant_methods = [m for m in all_methods if any(keyword in m.lower() for keyword in ['route', 'register', 'mount', 'include', 'add', 'chat', 'completion', 'impl', 'deployment'])]
    print(f"Relevant methods: {relevant_methods}")
    for method_name in relevant_methods:
        try:
            method = getattr(app, method_name)
            if callable(method):
                import inspect
                sig = inspect.signature(method)
                print(f"  {method_name}{sig}")
        except Exception as e:
            print(f"  {method_name}: Error - {e}")
    print("=" * 80)
    
    # Check if DIALApp has the impl stored
    if hasattr(app, 'impl'):
        logger.info(f"DIALApp.impl: {type(app.impl)}")
    if hasattr(app, '_impl'):
        logger.info(f"DIALApp._impl: {type(app._impl)}")
    if hasattr(app, 'deployment_name'):
        logger.info(f"DIALApp.deployment_name: {app.deployment_name}")
    if hasattr(app, '_deployment_name'):
        logger.info(f"DIALApp._deployment_name: {app._deployment_name}")
    
    # Try to access the underlying FastAPI app before adding middleware
    logger.info("Checking DIALApp structure before middleware...")
    if hasattr(app, 'app'):
        underlying_app = app.app
        logger.info(f"Found underlying app: {type(underlying_app)}")
        if hasattr(underlying_app, 'router'):
            logger.info("Checking underlying app router routes:")
            for route in underlying_app.router.routes:
                path = getattr(route, 'path', 'N/A')
                path_regex = getattr(route, 'path_regex', 'N/A')
                methods = getattr(route, 'methods', 'N/A')
                logger.info(f"  - {path} (regex: {path_regex}, methods: {methods})")
    
    # Add request logging middleware
    logger.info("Adding request logging middleware...")
    app.add_middleware(RequestLoggingMiddleware)
    
    # Check routes again after middleware
    logger.info("Checking routes after middleware...")
    if hasattr(app, 'router'):
        logger.info("Router routes after middleware:")
        for route in app.router.routes:
            path = getattr(route, 'path', 'N/A')
            methods = getattr(route, 'methods', 'N/A')
            logger.info(f"  - {path} (methods: {methods})")
    
    # Function to inspect routes - call it both at startup and after startup
    def inspect_routes():
        logger.info("=" * 80)
        logger.info("ROUTE INSPECTION")
        logger.info(f"DIALApp type: {type(app)}")
        logger.info(f"DIALApp class: {app.__class__.__name__}")
        
        # Try to access routes directly
        if hasattr(app, 'routes'):
            logger.info("Registered routes (app.routes):")
            for route in app.routes:
                path = getattr(route, 'path', 'N/A')
                methods = getattr(route, 'methods', 'N/A')
                route_type = type(route).__name__
                logger.info(f"  - {path} (methods: {methods}, type: {route_type})")
        else:
            logger.warning("Could not access app.routes")
        
        # Try to access underlying FastAPI app
        if hasattr(app, 'app'):
            logger.info(f"DIALApp wraps another app: {type(app.app)}")
            if hasattr(app.app, 'routes'):
                logger.info("Underlying app routes:")
                for route in app.app.routes:
                    path = getattr(route, 'path', 'N/A')
                    methods = getattr(route, 'methods', 'N/A')
                    logger.info(f"  - {path} (methods: {methods})")
            # Check if it's a FastAPI app and has router
            if hasattr(app.app, 'router'):
                logger.info(f"Underlying app router type: {type(app.app.router)}")
                if hasattr(app.app.router, 'routes'):
                    logger.info("Underlying app router routes:")
                    for route in app.app.router.routes:
                        path = getattr(route, 'path', 'N/A')
                        path_regex = getattr(route, 'path_regex', 'N/A')
                        methods = getattr(route, 'methods', 'N/A')
                        logger.info(f"  - {path} (regex: {path_regex}, methods: {methods})")
        
        # Try to access router directly
        if hasattr(app, 'router'):
            logger.info(f"DIALApp has router: {type(app.router)}")
            if hasattr(app.router, 'routes'):
                logger.info("Router routes:")
                for route in app.router.routes:
                    path = getattr(route, 'path', 'N/A')
                    methods = getattr(route, 'methods', 'N/A')
                    logger.info(f"  - {path} (methods: {methods})")
        
        # Check all attributes of DIALApp
        logger.info("DIALApp attributes (non-private):")
        for attr in dir(app):
            if not attr.startswith('_'):
                try:
                    value = getattr(app, attr)
                    if not callable(value) or attr in ['add_middleware', 'on_event']:
                        logger.info(f"  - {attr}: {type(value)}")
                except:
                    pass
        
        # Check if DIALApp is callable (ASGI app)
        if callable(app):
            logger.info("DIALApp is callable (ASGI app)")
        
        # Try to get the FastAPI instance
        try:
            import fastapi
            if isinstance(app, fastapi.FastAPI):
                logger.info("DIALApp IS a FastAPI instance")
            elif hasattr(app, '__class__'):
                logger.info(f"DIALApp MRO: {[cls.__name__ for cls in app.__class__.__mro__]}")
        except Exception as e:
            logger.debug(f"Could not check FastAPI type: {e}")
        
        logger.info("=" * 80)
    
    # Inspect routes at startup - use print to ensure it's visible
    print("=" * 80)
    print("INSPECTING ROUTES AT STARTUP (using print)")
    print("=" * 80)
    logger.info("Inspecting routes at startup...")
    inspect_routes()
    print("=" * 80)
    
    # Also inspect routes after app startup (using startup event)
    try:
        @app.on_event("startup")
        async def startup_event():
            print("=" * 80)
            print("STARTUP EVENT FIRED")
            print("=" * 80)
            logger.info("=" * 80)
            logger.info("STARTUP EVENT - Inspecting routes after startup...")
            inspect_routes()
            logger.info("=" * 80)
            print("=" * 80)
    except Exception as e:
        logger.error(f"Could not register startup event: {e}", exc_info=True)
    
    print("=" * 80)
    print("STARTING UVICORN SERVER")
    print("=" * 80)
    logger.info("Starting uvicorn server...")
    
    # Final check - try to find the route by checking all possible locations
    logger.info("=" * 80)
    logger.info("FINAL ROUTE CHECK")
    logger.info("=" * 80)
    
    # Check all routes including Mount objects
    logger.info("Checking all routes (including Mount objects):")
    for route in app.routes:
        route_type = type(route).__name__
        if route_type == 'Mount':
            logger.info(f"  MOUNT: {getattr(route, 'path', 'N/A')} -> {getattr(route, 'app', 'N/A')}")
            mounted_app = getattr(route, 'app', None)
            if mounted_app and hasattr(mounted_app, 'routes'):
                logger.info(f"    Mounted app routes:")
                for sub_route in mounted_app.routes:
                    sub_path = getattr(sub_route, 'path', 'N/A')
                    sub_methods = getattr(sub_route, 'methods', 'N/A')
                    logger.info(f"      - {sub_path} (methods: {sub_methods})")
        else:
            path = getattr(route, 'path', 'N/A')
            path_regex = getattr(route, 'path_regex', 'N/A')
            methods = getattr(route, 'methods', 'N/A')
            logger.info(f"  {route_type}: {path} (regex: {path_regex}, methods: {methods})")
    
    # Check if DIALApp has a _routes or routes attribute
    for attr_name in ['routes', '_routes', 'app', 'router', '_router', 'impl', '_impl']:
        if hasattr(app, attr_name):
            attr_value = getattr(app, attr_name)
            logger.info(f"Found attribute '{attr_name}': {type(attr_value)}")
            if hasattr(attr_value, 'routes'):
                routes = attr_value.routes
                logger.info(f"  Routes in {attr_name}:")
                for route in routes:
                    path = getattr(route, 'path', getattr(route, 'path_regex', 'N/A'))
                    methods = getattr(route, 'methods', 'N/A')
                    logger.info(f"    - {path} (methods: {methods})")
    
    logger.info("=" * 80)
    
    # CRITICAL: Check if DIALApp uses a catch-all route or routes via ASGI
    # DIALApp might register routes dynamically or use a different mechanism
    logger.info("=" * 80)
    logger.info("CRITICAL: Testing if route handler exists via ASGI")
    logger.info("=" * 80)
    
    # Try to access the ASGI app directly
    asgi_app = app
    logger.info(f"ASGI app type: {type(asgi_app)}")
    
    # Check if there's a way to test the route
    # DIALApp might use a catch-all or route via path parameters
    # Let's check if there's a route that matches our path pattern
    test_path = "/openai/deployments/calculations-agent/chat/completions"
    logger.info(f"Testing path: {test_path}")
    
    # Check if DIALApp has any route matching logic
    if hasattr(app, 'router'):
        # Try to find a route that could match
        for route in app.router.routes:
            route_path = getattr(route, 'path', '')
            route_regex = getattr(route, 'path_regex', None)
            if route_regex:
                match = route_regex.match(test_path)
                if match:
                    logger.info(f"FOUND MATCHING ROUTE: {route_path} matches {test_path}")
                    logger.info(f"  Route: {route}")
                    logger.info(f"  Match groups: {match.groups()}")
    
    logger.info("=" * 80)
    
    uvicorn.run(app, host="0.0.0.0", port=5001, log_level="debug")