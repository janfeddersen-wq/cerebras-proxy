import os
import json
import asyncio
import aiohttp
from aiohttp import web
import logging
from typing import Dict, Any, List
from datetime import datetime
from pathlib import Path
import uuid
import copy

from api_key_manager import ApiKeyManager


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Target API host
TARGET_API_HOST = "https://api.cerebras.ai/v1/"

# Error codes that trigger key rotation
ROTATE_KEY_ERROR_CODES = {429, 500}

# Request/Response logging configuration
LOG_REQUESTS_ENABLED = os.environ.get("LOG_REQUESTS", "true").lower() == "true"
LOG_DIR = os.environ.get("LOG_DIR", "./logs")


class ProxyServer:
    """
    A proxy server that forwards requests to the Cerebras API
    with round-robin API key rotation.
    """
    def __init__(self, api_key_manager: ApiKeyManager):
        self.api_key_manager = api_key_manager
        self.app = web.Application()
        # Add status endpoint
        self.app.router.add_get("/_status", self.status_handler)
        # Add the catch-all route (must be last)
        self.app.router.add_route("*", "/{path:.*}", self.proxy_handler)

        # Create logs directory if logging is enabled
        if LOG_REQUESTS_ENABLED:
            Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
            logger.info(f"Request/Response logging enabled. Logs will be saved to: {LOG_DIR}")

    def _sanitize_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Sanitize headers by removing sensitive information like API keys.
        """
        sanitized = dict(headers)
        # Remove or mask authorization headers
        if 'Authorization' in sanitized:
            sanitized['Authorization'] = '[REDACTED]'
        if 'authorization' in sanitized:
            sanitized['authorization'] = '[REDACTED]'
        return sanitized

    def _fix_missing_tool_responses(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates and fixes messages array to ensure all tool_calls have corresponding tool responses.
        If a tool_call is missing its response, injects a fake "failed" response.
        """
        # Only process chat completion requests with messages
        if 'messages' not in request_data or not isinstance(request_data['messages'], list):
            return request_data

        messages: List[Dict[str, Any]] = request_data['messages']
        fixed_messages: List[Dict[str, Any]] = []
        pending_tool_calls: List[str] = []  # Track tool_calls waiting for responses

        for i, msg in enumerate(messages):
            # Make a copy of the message to avoid modifying original
            msg_copy = copy.deepcopy(msg)

            # Check if this message has tool_calls
            if msg_copy.get('role') == 'assistant' and 'tool_calls' in msg_copy:
                # Add this message
                fixed_messages.append(msg_copy)
                # Track all tool_call IDs that need responses
                for tool_call in msg_copy.get('tool_calls', []):
                    if 'id' in tool_call:
                        pending_tool_calls.append(tool_call['id'])
                continue

            # Check if this is a tool response
            if msg_copy.get('role') == 'tool' and 'tool_call_id' in msg_copy:
                # Remove this tool_call_id from pending
                tool_call_id = msg_copy['tool_call_id']
                if tool_call_id in pending_tool_calls:
                    pending_tool_calls.remove(tool_call_id)
                fixed_messages.append(msg_copy)
                continue

            # If we have pending tool_calls and this is NOT a tool response,
            # inject fake responses for all pending tool_calls
            if pending_tool_calls:
                logger.warning(f"Found {len(pending_tool_calls)} tool_calls without responses. Injecting fake 'failed' responses.")
                for tool_call_id in pending_tool_calls:
                    fake_response = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": "failed"
                    }
                    fixed_messages.append(fake_response)
                    logger.info(f"Injected fake tool response for tool_call_id: {tool_call_id}")
                pending_tool_calls.clear()

            # Add the current message
            fixed_messages.append(msg_copy)

        # Handle any remaining pending tool_calls at the end
        if pending_tool_calls:
            logger.warning(f"Found {len(pending_tool_calls)} tool_calls without responses at end of messages. Injecting fake 'failed' responses.")
            for tool_call_id in pending_tool_calls:
                fake_response = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "failed"
                }
                fixed_messages.append(fake_response)
                logger.info(f"Injected fake tool response for tool_call_id: {tool_call_id}")

        # Create a deep copy of request_data and update messages
        fixed_request_data = copy.deepcopy(request_data)
        fixed_request_data['messages'] = fixed_messages
        return fixed_request_data

    async def _save_request_response_log(
        self,
        request_method: str,
        request_path: str,
        request_headers: Dict[str, str],
        request_body: bytes,
        response_status: int,
        response_headers: Dict[str, str],
        response_body: bytes,
        duration_ms: float
    ):
        """
        Save request and response data to a JSON file in the logs directory.
        """
        if not LOG_REQUESTS_ENABLED:
            return

        try:
            # Create timestamp-based filename
            timestamp = datetime.utcnow()
            date_dir = timestamp.strftime("%Y-%m-%d")
            timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S_%f")
            request_id = str(uuid.uuid4())[:8]

            # Sanitize path for filename (replace / with _)
            safe_path = request_path.replace('/', '_').replace('\\', '_')[:50]
            filename = f"{timestamp_str}_{request_method}_{safe_path}_{request_id}.json"

            # Create date subdirectory
            log_dir = Path(LOG_DIR) / date_dir
            log_dir.mkdir(parents=True, exist_ok=True)

            # Decode body if possible
            def decode_body(body: bytes) -> Any:
                if not body:
                    return None
                try:
                    # First, try to parse as JSON
                    return json.loads(body.decode('utf-8'))
                except json.JSONDecodeError:
                    # Not JSON, try to decode as plain text (e.g., SSE streaming)
                    try:
                        return body.decode('utf-8')
                    except UnicodeDecodeError:
                        # Not valid UTF-8, store as base64 binary
                        import base64
                        return {"_binary": base64.b64encode(body).decode('ascii')}
                except UnicodeDecodeError:
                    # Not valid UTF-8, store as base64 binary
                    import base64
                    return {"_binary": base64.b64encode(body).decode('ascii')}

            # Create log entry
            log_entry = {
                "timestamp": timestamp.isoformat(),
                "request_id": request_id,
                "request": {
                    "method": request_method,
                    "path": request_path,
                    "headers": self._sanitize_headers(request_headers),
                    "body": decode_body(request_body)
                },
                "response": {
                    "status": response_status,
                    "headers": dict(response_headers),
                    "body": decode_body(response_body)
                },
                "duration_ms": duration_ms
            }

            # Write to file
            log_file = log_dir / filename
            with open(log_file, 'w') as f:
                json.dump(log_entry, f, indent=2)

            logger.debug(f"Saved request/response log to {log_file}")

        except Exception as e:
            logger.error(f"Failed to save request/response log: {e}")

    async def status_handler(self, request: web.Request) -> web.Response:
        """
        Returns the current status of all API keys.
        """
        status = await self.api_key_manager.get_status()
        return web.json_response(status)

    async def proxy_handler(self, request: web.Request) -> web.Response:
        """
        Handles all incoming requests, forwards them to the target API,
        and returns the response. Implements smart retry logic with key rotation.
        """
        start_time = datetime.utcnow()

        path = request.match_info["path"]

        # Avoid /v1/v1 duplication if the request path already includes v1/
        if path.startswith("v1/"):
            path = path[3:]  # Remove the "v1/" prefix

        target_url = f"{TARGET_API_HOST}{path}"

        # Read request body once for both forwarding and logging
        request_body = await request.read()
        original_request_body = request_body

        # Apply tool_call validation fix for chat completion requests (ALWAYS ENABLED)
        if 'chat/completions' in path and request_body:
            try:
                request_data = json.loads(request_body.decode('utf-8'))
                original_msg_count = len(request_data.get('messages', []))

                fixed_request_data = self._fix_missing_tool_responses(request_data)
                fixed_msg_count = len(fixed_request_data.get('messages', []))

                # Only use the fixed body if we actually made changes
                if fixed_msg_count > original_msg_count:
                    # Serialize with compact separators to match original formatting
                    fixed_body = json.dumps(fixed_request_data, separators=(',', ':')).encode('utf-8')

                    # Validate the serialized JSON
                    test_parse = json.loads(fixed_body.decode('utf-8'))
                    if not isinstance(test_parse, dict):
                        raise ValueError("Serialized data is not a valid JSON object")

                    request_body = fixed_body
                    logger.info(f"Applied tool_call fix: {original_msg_count} -> {fixed_msg_count} messages (size: {len(original_request_body)} -> {len(fixed_body)} bytes)")
            except Exception as e:
                logger.error(f"Tool call fix failed: {e}", exc_info=True)
                request_body = original_request_body

        # Get headers AFTER body modification, excluding Authorization, Host, and Content-Length
        # Content-Length must be recalculated to match the (possibly modified) body
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in ('authorization', 'host', 'content-length')}
        headers["User-Agent"] = "Cerebras-Proxy/1.0"

        # Set correct Content-Length for the (possibly modified) body
        if request_body:
            headers["Content-Length"] = str(len(request_body))

        logger.info(f"Processing request to {target_url}")

        # Retry with automatic key rotation
        max_retries = self.api_key_manager.get_key_count() * 2  # Allow multiple passes through all keys

        for attempt in range(max_retries):
            # Get the current API key (will wait if all are rate-limited)
            api_key = await self.api_key_manager.get_current_key()
            headers["Authorization"] = f"Bearer {api_key}"

            try:
                # Use aiohttp client to make request
                async with aiohttp.ClientSession() as session:
                    # Prepare the request based on the method
                    method = request.method
                    if method in ("GET", "HEAD", "OPTIONS"):
                        async with session.request(method, target_url, headers=headers) as resp:
                            # Stream the response body back to the client
                            body = await resp.read()
                            # Create a new response with the target API's status and headers
                            response = web.Response(
                                status=resp.status,
                                body=body,
                                headers={key: value for key, value in resp.headers.items()
                                         if key.lower() not in ('content-length', 'transfer-encoding')}
                            )

                            # Handle rate limiting
                            if resp.status == 429:
                                logger.warning(f"Rate limited (429), marking key and switching...")
                                await self.api_key_manager.mark_key_rate_limited(api_key)
                                continue
                            elif resp.status == 500:
                                logger.warning(f"Server error (500), trying next key...")
                                await self.api_key_manager.mark_key_rate_limited(api_key)
                                continue
                            else:
                                # Success or non-retryable error
                                if resp.status < 400:
                                    await self.api_key_manager.mark_key_success(api_key)
                                logger.info(f"Request completed with status {resp.status}")

                                # Log request/response if enabled
                                end_time = datetime.utcnow()
                                duration_ms = (end_time - start_time).total_seconds() * 1000
                                await self._save_request_response_log(
                                    request_method=method,
                                    request_path=path,
                                    request_headers=dict(request.headers),
                                    request_body=original_request_body,
                                    response_status=resp.status,
                                    response_headers=dict(resp.headers),
                                    response_body=body,
                                    duration_ms=duration_ms
                                )

                                return response
                    else:
                        # For methods with bodies (POST, PUT, PATCH, DELETE)
                        # Use the fixed request body for forwarding
                        async with session.request(method, target_url, headers=headers,
                                                   data=request_body) as resp:
                            # Stream the response body back to the client
                            body = await resp.read()
                            response = web.Response(
                                status=resp.status,
                                body=body,
                                headers={key: value for key, value in resp.headers.items()
                                         if key.lower() not in ('content-length', 'transfer-encoding')}
                            )

                            # Handle rate limiting
                            if resp.status == 429:
                                logger.warning(f"Rate limited (429), marking key and switching...")
                                await self.api_key_manager.mark_key_rate_limited(api_key)
                                continue
                            elif resp.status == 500:
                                logger.warning(f"Server error (500), trying next key...")
                                await self.api_key_manager.mark_key_rate_limited(api_key)
                                continue
                            else:
                                # Success or non-retryable error
                                if resp.status < 400:
                                    await self.api_key_manager.mark_key_success(api_key)
                                logger.info(f"Request completed with status {resp.status}")

                                # Log request/response if enabled
                                end_time = datetime.utcnow()
                                duration_ms = (end_time - start_time).total_seconds() * 1000
                                await self._save_request_response_log(
                                    request_method=method,
                                    request_path=path,
                                    request_headers=dict(request.headers),
                                    request_body=original_request_body,
                                    response_status=resp.status,
                                    response_headers=dict(resp.headers),
                                    response_body=body,
                                    duration_ms=duration_ms
                                )

                                return response

            except aiohttp.ClientError as e:
                logger.error(f"Client error on attempt {attempt + 1}: {e}")
                # For client errors, try the next key
                await self.api_key_manager.mark_key_rate_limited(api_key)
                continue
            except Exception as e:
                logger.error(f"Unexpected error on attempt {attempt + 1}: {e}")
                # Return a 500 error to the client for unexpected issues
                return web.Response(status=500, text=f"Proxy error: {e}")

        # If we get here, all attempts failed
        logger.error("Maximum retry attempts exceeded.")
        return web.Response(status=503, text="Service unavailable: Maximum retries exceeded.")
        
    async def run(self, host: str = "0.0.0.0", port: int = 8080):
        """
        Starts the proxy server using the existing event loop.
        """
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        
        # Keep the server running
        while True:
            await asyncio.sleep(3600)  # Sleep for an hour, effectively running forever


# Example startup logic to load API keys from environment variable
# This would normally be handled by a configuration management system
# but is included here for demonstration purposes
async def main():
    """
    Main entry point for the proxy server.
    """
    logger.info("Starting main() function...")

    # Get the API keys from the environment variable
    api_keys_json = os.environ.get("CEREBRAS_API_KEYS", "{}")
    logger.info(f"Retrieved API keys JSON: {repr(api_keys_json)}")

    try:
        api_keys: Dict[str, str] = json.loads(api_keys_json)
        logger.info(f"Successfully parsed {len(api_keys)} API keys")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON for API keys: {e}")
        logger.error(f"Raw API keys value: {repr(api_keys_json)}")
        return

    # Get optional cooldown configuration
    cooldown_seconds = int(os.environ.get("CEREBRAS_COOLDOWN", "60"))
    logger.info(f"Cooldown period set to {cooldown_seconds} seconds")

    # Create the API key manager
    api_key_manager = ApiKeyManager(api_keys, cooldown_seconds=cooldown_seconds)
    logger.info("Created API key manager successfully")

    # Create and run the proxy server
    proxy = ProxyServer(api_key_manager)
    logger.info("About to call proxy.run() with proper event loop integration")
    await proxy.run()


if __name__ == "__main__":
    try:
        # Try to get the current event loop
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # If no loop is running, create a new one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    # Run the main function in the appropriate event loop
    loop.run_until_complete(main())