# Cerebras API Proxy Server

This is a Python-based proxy server designed to forward requests to the Cerebras API while managing multiple API keys through a round-robin rotation mechanism. It handles rate limiting (429) and server errors (500) by automatically rotating keys and retrying requests.

## Features

- **Smart API Key Management**:
  - Sticks with one key until it hits rate limits (429 errors)
  - Automatically switches to the next available key only when needed
  - Tracks cooldown periods for each key
  - **Waits and retries** if all keys are rate-limited instead of failing immediately
- **Request Forwarding**: Forwards all incoming HTTP requests to the Cerebras API endpoint (`https://api.cerebras.ai/v1/`).
- **Dynamic Authorization**: Injects the `Authorization: Bearer <api_key>` header dynamically for each request using the current key.
- **Intelligent Error Handling**:
  - Automatically rotates keys on `429 Too Many Requests` or `500 Internal Server Error` responses
  - Marks keys as temporarily unavailable and tracks when they can be retried
  - Waits for the next available key instead of immediately failing
- **Concurrency Support**: Built with `aiohttp` to efficiently handle multiple concurrent requests with thread-safe key rotation.
- **Status Monitoring**: Built-in `/_status` endpoint to monitor API key health and rotation state.
- **Request/Response Logging**: Filesystem logging to save all requests and responses as JSON files for auditing, debugging, or analysis.
- **Automatic Tool Call Validation**: Detects and fixes missing tool responses in chat completion requests by automatically injecting fake "failed" responses to maintain valid conversation flow.

## Requirements

- Python 3.7 or higher
- `aiohttp` library
- `Brotli` library (required for handling Brotli-compressed responses from Cerebras API)

Install the required libraries:
```bash
pip install aiohttp Brotli
```

Or install from requirements.txt:
```bash
pip install -r requirements.txt
```

## Configuration

The proxy server uses environment variables for configuration:

### Required Configuration

**`CEREBRAS_API_KEYS`**: JSON string containing your Cerebras API keys.

Example JSON format:
```json
{
  "key1": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "key2": "sk-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
  "key3": "sk-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
}
```

### Optional Configuration

**`CEREBRAS_COOLDOWN`**: Number of seconds to wait before retrying a rate-limited key (default: 60)

Example:
```bash
CEREBRAS_COOLDOWN=90
```

**`LOG_DIR`**: Directory to save request/response logs (default: ./logs)

Example:
```bash
LOG_DIR=/var/log/cerebras-proxy
```

## Usage

1. Set the `CEREBRAS_API_KEYS` environment variable with your JSON configuration:
   ```bash
   export CEREBRAS_API_KEYS='{"key1":"sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","key2":"sk-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy","key3":"sk-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"}'
   ```

2. Run the proxy server:
   ```bash
   python proxy_server.py
   ```
   
   By default, the server will start on `127.0.0.1:8080`. You can modify the host and port by adjusting the parameters in the `run()` method call within `proxy_server.py`.

## Docker Setup

This project includes Docker configuration for easy deployment.

### Using Docker

1. Build the Docker image:
   ```bash
   docker build -t cerebras-proxy .
   ```

2. Run the container with your API keys:
   ```bash
   docker run -p 18080:8080 -e CEREBRAS_API_KEYS='{"key1":"sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","key2":"sk-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"}' cerebras-proxy
   ```

### Using Docker Compose

1. Create a `.env` file in the project root with your API keys:
   ```bash
   CEREBRAS_API_KEYS={"key1":"sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","key2":"sk-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"}
   ```

2. Build and run with Docker Compose:
   ```bash
   docker-compose up --build
   ```

The proxy will be available at `http://localhost:18080`.

3. Make requests to the proxy server as you would to the Cerebras API, but use the proxy's address instead:
   
   For example, if the Cerebras API endpoint is:
   ```
   POST https://api.cerebras.ai/v1/chat/completions
   ```
   
   You would send your request to:
   ```
   POST http://127.0.0.1:8080/chat/completions
   ```

   The proxy will handle adding the appropriate `Authorization` header.

## How Smart Rotation Works

Unlike traditional round-robin proxies that switch keys on every request, this proxy uses intelligent key management:

1. **Sticky Keys**: The proxy sticks with one key and uses it for all requests until it encounters a problem
2. **Smart Switching**: Only rotates to the next key when receiving a 429 (rate limit) or 500 (server error)
3. **Cooldown Tracking**: Remembers when each key was rate-limited and won't try it again until the cooldown expires
4. **Automatic Waiting**: If all keys are rate-limited, the proxy waits for the soonest available key instead of failing
5. **State Persistence**: The current key position is maintained across requests, so you don't always start with key1

**Example Flow:**
```
Request 1-100:   Uses key1 (all successful)
Request 101:     key1 hits rate limit → switches to key2
Request 102-200: Uses key2 (all successful)
Request 201:     key2 hits rate limit → switches to key3
Request 202:     key3 hits rate limit → waits for key1 cooldown to expire
Request 203-300: Uses key1 again (cooldown expired)
```

## Monitoring

The proxy provides a built-in status endpoint to monitor API key health and rotation state.

### Status Endpoint

Access the status endpoint at:
```
GET http://localhost:18080/_status
```

Example response:
```json
{
  "keys": [
    {
      "name": "key1",
      "available": true,
      "rate_limited_for": 0,
      "error_count": 0
    },
    {
      "name": "key2",
      "available": false,
      "rate_limited_for": 45.2,
      "error_count": 3
    }
  ],
  "current_key": "key1"
}
```

**Fields:**
- `available`: Whether the key is currently available for use
- `rate_limited_for`: Seconds remaining until the key can be retried (0 if available)
- `error_count`: Number of consecutive errors for this key
- `current_key`: Name of the key currently being used

## Request/Response Logging

The proxy includes filesystem logging that is always enabled to save all requests and responses for auditing, debugging, or analysis purposes.

### Configuring Log Directory

By default, logs are saved to `./logs`. You can specify a custom log directory:
```bash
export LOG_DIR=/var/log/cerebras-proxy
```

### Log File Format

Each request/response pair is saved as a separate JSON file with the following naming convention:
```
YYYYMMDD_HHMMSS_microseconds_METHOD_path_requestid.json
```

Logs are organized in date-based subdirectories:
```
logs/
├── 2025-11-06/
│   ├── 20251106_143022_123456_POST_chat_completions_abc123de.json
│   ├── 20251106_143023_234567_POST_chat_completions_xyz789ab.json
│   └── ...
└── 2025-11-07/
    └── ...
```

### Log Entry Structure

Each log file contains:
```json
{
  "timestamp": "2025-11-06T14:30:22.123456",
  "request_id": "abc123de",
  "request": {
    "method": "POST",
    "path": "chat/completions",
    "headers": {
      "Content-Type": "application/json",
      "Authorization": "[REDACTED]"
    },
    "body": {
      "model": "llama3.1-70b",
      "messages": [...]
    }
  },
  "response": {
    "status": 200,
    "headers": {
      "Content-Type": "application/json"
    },
    "body": {
      "id": "chat-...",
      "choices": [...]
    }
  },
  "duration_ms": 1234.56
}
```

### Privacy and Security

- **Authorization headers are automatically redacted** in logs to prevent API key leakage
- Binary data is base64-encoded if the body is not valid JSON or UTF-8
- Logs are stored locally and never transmitted elsewhere
- Consider disk space when enabling logging for high-traffic deployments

## Automatic Tool Call Validation

The proxy automatically detects and fixes invalid tool call sequences in chat completion requests. This prevents API errors when clients fail to provide responses for tool calls.

### How It Works

When processing `/chat/completions` requests, the proxy:

1. **Scans the message array** for assistant messages containing `tool_calls`
2. **Tracks pending tool calls** that are waiting for responses
3. **Detects missing responses** when:
   - A tool call is followed by a non-tool message (like a user message)
   - A tool call appears at the end of the messages array with no response
4. **Automatically injects fake responses** with `content: "failed"` for each missing tool call
5. **Updates Content-Length** header to match the modified request body

### Example

**Before (Invalid - Would cause 422 error):**
```json
{
  "messages": [
    {
      "role": "assistant",
      "tool_calls": [{"id": "call_123", "function": {...}}]
    },
    {
      "role": "user",
      "content": "test"
    }
  ]
}
```

**After (Valid - Automatically fixed by proxy):**
```json
{
  "messages": [
    {
      "role": "assistant",
      "tool_calls": [{"id": "call_123", "function": {...}}]
    },
    {
      "role": "tool",
      "tool_call_id": "call_123",
      "content": "failed"
    },
    {
      "role": "user",
      "content": "test"
    }
  ]
}
```

### Logging

When the fix is applied, you'll see log messages:
```
WARNING:__main__:Found 1 tool_calls without responses. Injecting fake 'failed' responses.
INFO:__main__:Injected fake tool response for tool_call_id: call_123
INFO:__main__:Applied tool_call fix: 2 -> 3 messages (size: 1234 -> 1456 bytes)
```

This feature is **always enabled** for all chat completion requests and requires no configuration.

## Architecture

This proxy server is built using the `aiohttp` framework for high performance and concurrency. It consists of two main components:

1.  **`ApiKeyManager` (`api_key_manager.py`)**: Manages the pool of API keys with intelligent rotation:
    - Tracks each key's state (available vs. rate-limited)
    - Maintains the current active key instead of rotating on every request
    - Only switches keys when a rate limit (429) or server error (500) is encountered
    - Implements automatic wait/retry when all keys are temporarily unavailable
    - Uses `asyncio.Lock` for thread-safe operation across concurrent requests

2.  **`ProxyServer` (`proxy_server.py`)**: The main application that listens for HTTP requests:
    - Catch-all route forwards requests to the Cerebras API
    - Integrates with `ApiKeyManager` to get the current key and handle rotation
    - Automatically retries with the next available key on failures
    - Provides a `/_status` endpoint for monitoring key health

## Error Handling

The proxy implements smart error handling with automatic recovery:

- **Rate Limiting (429)**: When a key hits rate limits:
  1. The key is marked as unavailable with a cooldown period (default 60 seconds)
  2. The proxy automatically switches to the next available key
  3. The request is retried immediately with the new key
  4. After the cooldown period, the key becomes available again

- **Server Errors (500)**: Treated similarly to rate limits:
  1. Key is marked as temporarily unavailable
  2. Automatic rotation to the next available key
  3. Request retry with the new key

- **All Keys Rate-Limited**: If all keys are temporarily unavailable:
  1. The proxy calculates which key will become available soonest
  2. Waits for that cooldown period
  3. Automatically retries the request when a key becomes available
  4. No immediate `503` error - the proxy handles waiting for you

- **Other Errors**: Non-retryable errors (4xx client errors, network issues) are returned to the client immediately

- **Maximum Retries**: After (number of keys × 2) attempts, returns `503 Service Unavailable`

## License

This project is licensed under the MIT License - see the LICENSE file for details.