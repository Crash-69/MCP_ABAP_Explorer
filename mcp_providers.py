"""Provider MCP con trasporti isolati e configurazione esterna."""

from __future__ import annotations

import json
import os
import re
import threading
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import AsyncContextManager, AsyncIterator, Callable
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / 'mcp_servers.json'
ENV_REFERENCE = re.compile(r'^\$\{([A-Z0-9_]+)(?::-([^}]*))?\}$')
SENSITIVE_ENV_NAMES = re.compile(r'PASSWORD|PASSWD|USER|TOKEN|SECRET|CREDENTIAL|PRIVATE.?KEY', re.IGNORECASE)
UI_CONNECTION_KEYS = {'SAP_URL', 'SAP_CLIENT', 'SAP_SYSNR', 'SAP_SYSTEM_ID'}
UI_CREDENTIAL_KEYS = {'SAP_USERNAME', 'SAP_PASSWORD'}
REQUIRED_ONPREM_KEYS = UI_CONNECTION_KEYS | UI_CREDENTIAL_KEYS
UI_OPTION_KEYS = UI_CONNECTION_KEYS | UI_CREDENTIAL_KEYS


class McpProviderError(RuntimeError):
    """Errore di configurazione o connessione MCP senza valori segreti."""


class SafeErrorWriter:
    def __init__(self, secrets: list[str]):
        self.secrets = secrets
        self.parts = []
        self.read_fd, write_fd = os.pipe()
        self.sink = os.fdopen(write_fd, 'wb', buffering=0)
        self.reader = threading.Thread(target=self._read_pipe, daemon=True)
        self.reader.start()

    def fileno(self) -> int:
        return self.sink.fileno()

    def write(self, value: str) -> int:
        written = len(value)
        self._append(value)
        return written

    def _read_pipe(self) -> None:
        with os.fdopen(self.read_fd, 'rb') as source:
            while chunk := source.read(2048):
                self._append(chunk.decode('utf-8', errors='replace'))

    def _append(self, value: str) -> None:
        self.parts.append(value)
        if sum(map(len, self.parts)) > 16384:
            self.parts = [''.join(self.parts)[-16384:]]

    def flush(self) -> None:
        self.sink.flush()

    def close(self) -> None:
        if not self.sink.closed:
            self.sink.close()
        self.reader.join(timeout=2)

    def message(self) -> str:
        message = ''.join(self.parts)
        for secret in self.secrets:
            message = message.replace(secret, '[REDACTED]')
        message = re.sub(r'(?i)(authorization\s*:\s*basic)\s+\S+', r'\1 [REDACTED]', message)
        message = re.sub(r'(?i)(authorization\s*:\s*bearer)\s+\S+', r'\1 [REDACTED]', message)
        return message.strip()


class IMcpProvider(ABC):
    allow_dynamic_tools = False

    def __init__(self, server_id: str, label: str):
        self.server_id = server_id
        self.label = label

    @abstractmethod
    def connect(self) -> AsyncContextManager[ClientSession]:
        """Apre una sessione inizializzata verso il server MCP."""

    async def discover_tools(self) -> dict:
        tools = {}
        async with self.connect() as session:
            cursor = None
            while True:
                page = await session.list_tools(cursor=cursor)
                tools.update({tool.name: tool for tool in page.tools})
                cursor = page.nextCursor
                if not cursor:
                    return tools

    async def execute_tool(self, name: str, arguments: dict):
        async with self.connect() as session:
            return await session.call_tool(name=name, arguments=arguments)


class CloudMcpProvider(IMcpProvider):
    def __init__(self, server_id: str, config: dict):
        super().__init__(server_id, config['label'])
        self.endpoint = resolve_value(config['endpoint'])
        if not self.endpoint.startswith(('https://', 'http://127.0.0.1', 'http://localhost')):
            raise McpProviderError('Endpoint SAP Cloud MCP non valido.')

    @asynccontextmanager
    async def connect(self):
        with anyio.fail_after(90):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=15), follow_redirects=True,
            ) as client:
                async with streamable_http_client(self.endpoint, http_client=client) as streams:
                    async with ClientSession(
                        streams[0], streams[1], read_timeout_seconds=timedelta(seconds=60),
                    ) as session:
                        await session.initialize()
                        yield session


class OnPremiseMcpProvider(IMcpProvider):
    allow_dynamic_tools = True

    def __init__(self, server_id: str, config: dict, connection_options: dict | None = None):
        super().__init__(server_id, config['label'])
        if connection_options is not None and not isinstance(connection_options, dict):
            raise McpProviderError('Le opzioni di connessione SAP devono essere un oggetto.')
        self.command = config['command']
        self.args = config.get('args', [])
        defaults = config.get('connection_defaults', {})
        self.env = resolve_environment(config.get('env', {}), {**defaults, **(connection_options or {})}, connection_options)
        self._secret_values = [
            value for key, value in self.env.items()
            if SENSITIVE_ENV_NAMES.search(key) and value
        ]

    @asynccontextmanager
    async def connect(self):
        parameters = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        stderr = SafeErrorWriter(self._secret_values)
        with anyio.fail_after(90):
            try:
                async with stdio_client(parameters, errlog=stderr) as streams:
                    async with ClientSession(
                        streams[0], streams[1], read_timeout_seconds=timedelta(seconds=60),
                    ) as session:
                        await session.initialize()
                        yield session
            except TimeoutError as error:
                raise McpProviderError('Timeout durante la connessione al server SAP On-Premise.') from error
            except Exception as error:
                stderr.close()
                detail = stderr.message()
                if detail:
                    raise McpProviderError(detail) from error
                safe_message = str(error)
                for secret in self._secret_values:
                    safe_message = safe_message.replace(secret, '[REDACTED]')
                raise McpProviderError(safe_message or 'Connessione al server SAP On-Premise non riuscita.') from error
            finally:
                stderr.close()

    def redact(self, value):
        if isinstance(value, str):
            for secret in self._secret_values:
                value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        return value

    async def execute_tool(self, name: str, arguments: dict):
        result = await super().execute_tool(name, arguments)
        from mcp.types import CallToolResult
        return CallToolResult.model_validate(self.redact(result.model_dump(mode='json')))


def _load_config() -> dict:
    path = Path(os.environ.get('MCP_SERVERS_CONFIG', str(DEFAULT_CONFIG_PATH))).expanduser()
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise McpProviderError(f'Configurazione MCP non disponibile o non valida: {path.name}.') from error
    if not isinstance(config, dict) or not isinstance(config.get('servers'), dict):
        raise McpProviderError('La configurazione MCP deve contenere la mappa servers.')
    return config


def resolve_value(value: str) -> str:
    match = ENV_REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return value
    key, default = match.groups()
    resolved = os.environ.get(key, default)
    if not resolved:
        raise McpProviderError(f'Variabile di ambiente MCP richiesta non configurata: {key}.')
    return resolved


def resolve_environment(
    raw_env: dict, defaults: dict | None = None, connection_options: dict | None = None,
) -> dict[str, str]:
    if not isinstance(raw_env, dict):
        raise McpProviderError('La configurazione env del server On-Premise non e valida.')
    resolved = {}
    for key, value in raw_env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise McpProviderError('La configurazione env del server On-Premise non e valida.')
        reference = ENV_REFERENCE.fullmatch(value)
        if SENSITIVE_ENV_NAMES.search(key) and (not reference or reference.group(2) is not None):
            raise McpProviderError(f'La credenziale {key} deve provenire da una variabile di ambiente senza fallback.')
        try:
            resolved[key] = resolve_value(value)
        except McpProviderError:
            if connection_options is not None and key in connection_options:
                resolved[key] = ''
            elif key in UI_CONNECTION_KEYS:
                resolved[key] = ''
            else:
                raise
    options = {**(defaults or {}), **(connection_options or {})}
    if not isinstance(options, dict) or set(options) - UI_OPTION_KEYS:
        raise McpProviderError('Le opzioni di connessione SAP non sono valide.')
    for key, value in options.items():
        max_length = 512 if key == 'SAP_PASSWORD' else 255
        if not isinstance(value, str) or len(value) > max_length or any(ord(char) < 32 for char in value):
            raise McpProviderError(f'Valore di connessione non valido per {key}.')
        if key == 'SAP_URL':
            try:
                parsed_url = urlsplit(value.strip())
                valid_url = (
                    parsed_url.scheme in {'http', 'https'} and parsed_url.hostname
                    and (parsed_url.port is None or parsed_url.port > 0)
                    and not parsed_url.username and not parsed_url.password
                    and not parsed_url.query and not parsed_url.fragment
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise McpProviderError('SAP_URL deve usare HTTP o HTTPS ed essere valido, senza credenziali o query.')
        if key == 'SAP_CLIENT' and not re.fullmatch(r'\d{3}', value.strip()):
            raise McpProviderError('Il client SAP deve contenere tre cifre.')
        if key == 'SAP_SYSNR' and not re.fullmatch(r'\d{2}', value.strip()):
            raise McpProviderError('Il numero istanza SAP deve contenere due cifre.')
        if key == 'SAP_SYSTEM_ID' and not re.fullmatch(r'[A-Za-z0-9]{3}', value.strip()):
            raise McpProviderError('L’ID sistema SAP deve contenere tre caratteri alfanumerici.')
        if key == 'SAP_SYSTEM_ID':
            value = value.upper()
        if key in UI_CREDENTIAL_KEYS and connection_options is not None:
            if not value or (key == 'SAP_USERNAME' and not value.strip()):
                raise McpProviderError(f'Inserire {key} per la richiesta ADT.')
            resolved[key] = value if key == 'SAP_PASSWORD' else value.strip()
        elif value.strip():
            resolved[key] = value.strip()
    if connection_options is not None:
        missing_ui_credentials = UI_CREDENTIAL_KEYS - set(connection_options)
        if missing_ui_credentials:
            raise McpProviderError('Inserire USER e password SAP nella UI per la richiesta ADT.')
    missing = sorted(key for key in REQUIRED_ONPREM_KEYS if not resolved.get(key))
    if missing:
        raise McpProviderError('Parametri SAP On-Premise mancanti: ' + ', '.join(missing) + '.')
    return resolved


def available_servers() -> list[dict[str, str]]:
    config = _load_config()
    servers = config['servers']
    default_server = config.get('default_server', 'sap-cloud')
    if default_server not in servers:
        raise McpProviderError('Il server MCP predefinito non e configurato.')
    return [
        {
            'id': server_id, 'label': item.get('label', server_id),
            'transport': item.get('transport'),
            **({'connection_defaults': item.get('connection_defaults', {})}
               if item.get('transport') == 'stdio' else {}),
        }
        for server_id, item in servers.items()
        if isinstance(server_id, str) and isinstance(item, dict)
    ]


def default_server_id() -> str:
    config = _load_config()
    server_id = os.environ.get('MCP_DEFAULT_SERVER', config.get('default_server', 'sap-cloud'))
    if server_id not in config['servers']:
        raise McpProviderError('Il server MCP predefinito non e configurato.')
    return server_id


def create_provider(server_id: str | None = None, connection_options: dict | None = None) -> IMcpProvider:
    config = _load_config()
    if server_id is not None and not isinstance(server_id, str):
        raise McpProviderError('Server MCP selezionato non valido.')
    selected_id = server_id or os.environ.get('MCP_DEFAULT_SERVER', config.get('default_server', 'sap-cloud'))
    server_config = config['servers'].get(selected_id)
    if not isinstance(server_config, dict):
        raise McpProviderError('Server MCP selezionato non configurato.')
    transport = server_config.get('transport')
    providers = {
        'streamable_http': CloudMcpProvider,
        'stdio': OnPremiseMcpProvider,
    }
    provider_type = providers.get(transport)
    if provider_type is None:
        raise McpProviderError('Trasporto MCP non supportato nella configurazione.')
    try:
        if transport == 'stdio':
            return provider_type(selected_id, server_config, connection_options)
        return provider_type(selected_id, server_config)
    except McpProviderError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise McpProviderError('Configurazione del server MCP non valida.') from error
