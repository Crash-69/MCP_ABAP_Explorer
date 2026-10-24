"""Diagnostica SAP ADT e MCP senza conservare credenziali nel report."""

from __future__ import annotations

import logging
import os
import re
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit

import anyio
import httpx

from mcp_providers import McpProviderError, create_provider

LOGGER = logging.getLogger('mcp.diagnostics')
REQUIRED_ENV = ('SAP_URL', 'SAP_USERNAME', 'SAP_PASSWORD', 'SAP_CLIENT')
ADT_ROOT_PATH = '/sap/bc/adt'
ADT_DISCOVERY_PATH = ADT_ROOT_PATH + '/discovery'


def _check(status: str, details: dict | None = None) -> dict:
    return {'status': status, **(details or {})}


def _status(values: list[str]) -> str:
    if any(value in {'ERROR', 'NOT_FOUND', 'FORBIDDEN', 'SSL_ERROR', 'UNREACHABLE', 'INSECURE_URL'} for value in values):
        return 'ERROR'
    if any(value in {'UNKNOWN', 'SKIPPED'} for value in values):
        return 'UNKNOWN'
    return 'OK'


def _overall(report: dict) -> tuple[str, str]:
    mcp_ready = (
        report['mcpStartup']['status'] == 'OK'
        and report['toolDiscovery']['status'] == 'OK'
    )
    sap_verified = (
        report['network']['hostReachable']['status'] == 'OK'
        and report['adt']['status'] == 'OK'
        and report['authentication']['status'] == 'OK'
    )
    if mcp_ready and not sap_verified and report['adt']['status'] in {'UNREACHABLE', 'SSL_ERROR'}:
        return 'PARTIAL', 'MCP operativo; connettività SAP non verificata.'
    if mcp_ready and sap_verified:
        return 'OK', 'MCP e endpoint ADT verificati.'
    return _status([
        report['environment']['status'], report['network']['status'], report['adt']['status'],
        report['authentication']['status'], report['mcpStartup']['status'],
        report['toolDiscovery']['status'], report['adtPermissions']['status'],
    ]), 'Uno o più controlli MCP/ADT non sono riusciti.'


def _safe_error(error: Exception, secrets: list[str]) -> str:
    message = str(error) or type(error).__name__
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        message = message.replace(secret, '[REDACTED]')
    message = re.sub(r'(?i)(authorization\s*:\s*basic)\s+\S+', r'\1 [REDACTED]', message)
    message = re.sub(r'(?i)(authorization\s*:\s*bearer)\s+\S+', r'\1 [REDACTED]', message)
    return message[:1200]


def _suggestions(report: dict) -> list[str]:
    suggestions = []
    for item in report['environment']['checks']:
        if item['status'] == 'ERROR':
            suggestions.append(f"Configurare {item['name']} nella UI o nell'ambiente del processo.")
    if report['network']['dns']['status'] == 'ERROR':
        suggestions.append('Verificare DNS, VPN e risoluzione del nome host SAP.')
    if report['network']['hostReachable']['status'] == 'ERROR':
        suggestions.append('Verificare rete, firewall e porta HTTP(S) del sistema SAP.')
    adt_status = report['adt']['status']
    if adt_status == 'NOT_FOUND':
        suggestions.append('Attivare il servizio SAP ADT su /sap/bc/adt/discovery.')
    elif adt_status == 'FORBIDDEN':
        suggestions.append('Verificare autorizzazioni SAP per ADT e accesso HTTP.')
    elif adt_status == 'SSL_ERROR':
        suggestions.append('Verificare certificato, trust store e catena TLS del sistema SAP.')
    elif adt_status == 'UNREACHABLE':
        suggestions.append('Verificare URL SAP e raggiungibilita del sistema dalla macchina locale.')
    elif adt_status == 'INSECURE_URL':
        suggestions.append('Usare https:// in SAP_URL; le credenziali non vengono inviate su HTTP.')
    if report['authentication']['status'] == 'ERROR':
        suggestions.append('Verificare USER e password SAP; non inserirli nei log o nei file di configurazione.')
    if report['mcpStartup']['status'] == 'ERROR':
        suggestions.append('Verificare percorso e permessi di abap-mcp-windows.exe e stderr del server MCP.')
    if report['adtPermissions']['status'] == 'ERROR':
        suggestions.append('Verificare i ruoli e le autorizzazioni ADT dell’utente SAP.')
    if report['toolDiscovery']['status'] == 'ERROR':
        suggestions.append('Verificare la versione di abap-mcp e la disponibilita dell’endpoint ADT.')
    return list(dict.fromkeys(suggestions))


async def _check_network(host: str, port: int) -> tuple[dict, dict]:
    try:
        with anyio.fail_after(5):
            addresses = await anyio.to_thread.run_sync(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        dns = _check('OK', {'addressesFound': len(addresses)})
    except Exception as error:
        LOGGER.warning('SAP diagnostics DNS failed: %s', type(error).__name__)
        return _check('ERROR', {'error': type(error).__name__}), _check('SKIPPED')
    try:
        with anyio.fail_after(5):
            connection = await anyio.to_thread.run_sync(lambda: socket.create_connection((host, port), timeout=4))
            await anyio.to_thread.run_sync(connection.close)
        reachable = _check('OK')
    except Exception as error:
        LOGGER.warning('SAP diagnostics TCP reachability failed: %s', type(error).__name__)
        reachable = _check('ERROR', {'error': type(error).__name__})
    return dns, reachable


async def diagnose_sap_onpremise(
    connection_options: dict,
    provider_factory=None,
    http_client_factory=httpx.AsyncClient,
) -> dict:
    """Esegue controlli SAP espliciti; il report non include credenziali."""
    source = dict(connection_options) if isinstance(connection_options, dict) else {}
    values = {key: source.get(key, os.environ.get(key, '')) for key in REQUIRED_ENV}
    for optional_key in ('SAP_SYSNR', 'SAP_SYSTEM_ID'):
        if source.get(optional_key):
            values[optional_key] = source[optional_key]
    secrets = [values['SAP_PASSWORD'], values['SAP_USERNAME']]
    environment_checks = [
        {'name': key, 'status': 'OK' if isinstance(values[key], str) and bool(values[key].strip()) else 'ERROR'}
        for key in REQUIRED_ENV
    ]
    environment_status = _status([item['status'] for item in environment_checks])
    report = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'environment': {'status': environment_status, 'checks': environment_checks},
        'network': {'status': 'UNKNOWN', 'dns': _check('SKIPPED'), 'hostReachable': _check('SKIPPED')},
        'adt': {'status': 'SKIPPED', 'endpoint': None, 'httpStatus': None, 'error': None},
        'authentication': {'status': 'SKIPPED', 'message': 'Credenziali o URL mancanti.'},
        'adtPermissions': {'status': 'SKIPPED', 'message': None},
        'mcpStartup': {'status': 'SKIPPED', 'error': None},
        'toolDiscovery': {'status': 'SKIPPED', 'toolsFound': 0, 'tools': [], 'error': None},
        'toolsFound': 0,
        'suggestions': [],
    }
    if environment_status == 'ERROR':
        report['mcpStartup']['error'] = 'Variabili ambiente richieste mancanti.'
        report['toolDiscovery']['error'] = 'Discovery saltata: configurazione incompleta.'
        report['summary'] = 'ERROR'
        report['suggestions'] = _suggestions(report)
        return report

    try:
        parsed_url = urlsplit(values['SAP_URL'].strip())
        if parsed_url.scheme not in {'http', 'https'} or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError('SAP_URL deve essere un URL HTTP(S) senza credenziali.')
        port = parsed_url.port or (443 if parsed_url.scheme == 'https' else 80)
        base_url = values['SAP_URL'].strip().rstrip('/')
        url_path = parsed_url.path.rstrip('/')
        if url_path.endswith(ADT_DISCOVERY_PATH):
            adt_endpoint = base_url
        elif url_path.endswith(ADT_ROOT_PATH):
            adt_endpoint = base_url + '/discovery'
        else:
            adt_endpoint = base_url + ADT_DISCOVERY_PATH
    except ValueError as error:
        report['adt'] = {'status': 'UNREACHABLE', 'endpoint': None, 'httpStatus': None, 'error': str(error)}
        report['network']['status'] = 'ERROR'
        report['authentication'] = {'status': 'SKIPPED', 'message': 'URL SAP non valido.'}
        report['adtPermissions'] = {'status': 'SKIPPED', 'message': None}
        report['mcpStartup']['error'] = 'URL SAP non valido.'
        report['toolDiscovery']['error'] = 'Discovery saltata: URL non valido.'
        report['summary'] = 'ERROR'
        report['suggestions'] = ['Impostare SAP_URL con schema e host validi, ad esempio https://sap.example.local.']
        return report

    host = parsed_url.hostname
    report['network']['dns'], report['network']['hostReachable'] = await _check_network(host, port)
    report['network']['status'] = _status([
        report['network']['dns']['status'], report['network']['hostReachable']['status'],
    ])
    report['adt']['endpoint'] = adt_endpoint
    LOGGER.info('SAP diagnostics ADT request started for host=%s', host)
    try:
        with anyio.fail_after(12):
            async with http_client_factory(
                timeout=httpx.Timeout(8, connect=4), verify=True,
                follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.get(
                    adt_endpoint,
                    params={'sap-client': values['SAP_CLIENT']},
                    headers={'Accept': 'application/atomsvc+xml'},
                    auth=httpx.BasicAuth(values['SAP_USERNAME'], values['SAP_PASSWORD']),
                )
        code = response.status_code
        report['adt']['httpStatus'] = code
        if code not in {200, 401} and response.text:
            report['adt']['message'] = _safe_error(RuntimeError(response.text), secrets)[:1000]
        if code in {200, 401}:
            report['adt']['status'] = 'OK'
        elif code == 403:
            report['adt']['status'] = 'FORBIDDEN'
        elif code == 404:
            report['adt']['status'] = 'NOT_FOUND'
        else:
            report['adt']['status'] = 'ERROR'
        if code == 200:
            report['authentication'] = {'status': 'OK', 'message': 'Autenticazione accettata.'}
        elif code == 401:
            report['authentication'] = {'status': 'ERROR', 'message': 'HTTP 401: credenziali non accettate.'}
        elif code == 403:
            report['authentication'] = {'status': 'UNKNOWN', 'message': 'HTTP 403: richiesta negata da SAP.'}
            report['adtPermissions'] = {'status': 'ERROR', 'message': 'HTTP 403: accesso ADT negato.'}
        else:
            report['authentication'] = {'status': 'SKIPPED', 'message': f'HTTP {code}.'}
        if code == 200:
            report['adtPermissions'] = {
                'status': 'OK',
                'message': 'Endpoint accessibile; i permessi sulle singole operazioni non sono ancora verificati.',
            }
        LOGGER.info('SAP diagnostics ADT completed: http_status=%s', code)
    except httpx.ConnectError as error:
        ssl_error = isinstance(error.__cause__, ssl.SSLError)
        report['adt'].update(status='SSL_ERROR' if ssl_error else 'UNREACHABLE', error=type(error).__name__)
        report['authentication'] = {'status': 'SKIPPED', 'message': 'Handshake TLS fallito.' if ssl_error else 'Server non raggiungibile.'}
        LOGGER.warning('SAP diagnostics ADT connection failed: %s', type(error).__name__)
    except (httpx.ConnectTimeout, TimeoutError) as error:
        report['adt'].update(status='UNREACHABLE', error=type(error).__name__)
        report['authentication'] = {'status': 'SKIPPED', 'message': 'Server non raggiungibile.'}
        LOGGER.warning('SAP diagnostics ADT connection failed: %s', type(error).__name__)
    except httpx.TimeoutException as error:
        report['adt'].update(status='UNREACHABLE', error=type(error).__name__)
        report['authentication'] = {'status': 'SKIPPED', 'message': 'Timeout HTTP.'}
        LOGGER.warning('SAP diagnostics ADT timeout: %s', type(error).__name__)
    except httpx.TransportError as error:
        report['adt'].update(status='ERROR', error=type(error).__name__)
        report['authentication'] = {'status': 'SKIPPED', 'message': 'Errore di trasporto HTTP.'}
        LOGGER.warning('SAP diagnostics ADT transport failed: %s', type(error).__name__)
    except Exception as error:
        report['adt'].update(status='ERROR', error=type(error).__name__)
        report['authentication'] = {'status': 'SKIPPED', 'message': 'Richiesta ADT non completata.'}
        LOGGER.error('SAP diagnostics ADT unexpected failure: error_type=%s', type(error).__name__)

    selected_factory = provider_factory or create_provider
    mcp_started = False
    try:
        provider = selected_factory('sap-on-premise', values)
        LOGGER.info('SAP diagnostics MCP startup started: server_id=sap-on-premise')
        async with provider.connect() as session:
            mcp_started = True
            report['mcpStartup'] = {'status': 'OK', 'error': None}
            tools = {}
            cursor = None
            while True:
                page = await session.list_tools(cursor=cursor)
                tools.update({tool.name: tool for tool in page.tools})
                cursor = page.nextCursor
                if not cursor:
                    break
        tool_names = sorted(tools)
        report['toolsFound'] = len(tool_names)
        report['toolDiscovery'] = {
            'status': 'OK' if tool_names else 'ERROR',
            'toolsFound': len(tool_names), 'tools': tool_names,
            'error': None if tool_names else 'Il server MCP non ha restituito tool.',
        }
        if not tool_names and report['adtPermissions']['status'] != 'ERROR':
            report['adtPermissions'] = {'status': 'UNKNOWN', 'message': 'Nessun tool trovato; verificare autorizzazioni ADT.'}
        LOGGER.info('SAP diagnostics MCP discovery completed: tools_found=%s names=%s', len(tool_names), ','.join(tool_names))
    except Exception as error:
        error_text = _safe_error(error, secrets)
        report['mcpStartup'] = {
            'status': 'OK' if mcp_started else 'ERROR',
            'error': None if mcp_started else error_text,
        }
        report['toolDiscovery'] = {'status': 'ERROR', 'toolsFound': 0, 'tools': [], 'error': error_text}
        if any(token in error_text.lower() for token in ('403', 'forbidden', 'permission', 'authorization', 'autorizz')):
            report['adtPermissions'] = {'status': 'ERROR', 'message': error_text}
        LOGGER.error('SAP diagnostics MCP startup/discovery failed: error_type=%s', type(error).__name__)

    report['summary'], report['summaryMessage'] = _overall(report)
    report['suggestions'] = _suggestions(report)
    if parsed_url.scheme == 'http':
        report['suggestions'].append('HTTP non cifra credenziali e dati SAP: usare solo su rete fidata; preferire HTTPS.')
    return report
