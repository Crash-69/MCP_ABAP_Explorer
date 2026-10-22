"""Client MCP deterministico ed esportazione locale con verifica Magika."""

from __future__ import annotations

import json
import math
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import anyio
import httpx
from jsonschema import Draft202012Validator
from magika import Magika
from mcp_providers import McpProviderError, available_servers, create_provider, default_server_id
from typing import Callable

ProviderFactory = Callable[[str | None], object]

ENDPOINT = 'https://mcp-abap.marianzeis.de/mcp'
MAX_CONTENT = 5 * 1024 * 1024
MAGIKA_LOCK = threading.Lock()
MIN_SCORE = 0.06
MAX_CODE_BYTES = 50 * 1024
TOOLS = {
    'search': 'Cerca Documentazione',
    'fetch': 'Recupera Documento Completo',
    'abap_lint': 'Analisi Statica Codice',
    'abap_feature_matrix': 'Matrice Funzionalita ABAP',
    'sap_community_search': 'Cerca nella SAP Community',
    'sap_search_objects': 'Cerca Oggetti Rilasciati SAP',
    'sap_get_object_details': 'Dettagli Oggetti Rilasciati',
    'sap_discovery_center_search': 'Cerca Servizi SAP BTP',
    'sap_discovery_center_service': 'Dettagli Servizio SAP BTP',
}
TOOL_TAG = re.compile(r'(?<![\w@])@[a-z][\w-]*\b', re.IGNORECASE)
QUOTED_TEXT = re.compile(
    r'"([^"\n]+)"|(?<!\w)\'([^\'\n]+)\'(?!\w)|'
    r'\u201c([^\u201d\n]+)\u201d|\u00ab([^\u00bb\n]+)\u00bb|\u2018([^\u2019\n]+)\u2019'
)
FILLER = re.compile(
    r'^\s*(?:(?:per\s+favore|puoi|potresti)\s+)*'
    r'(?:cerca|cercare|trova|trovare)\s+'
    r'(?:(?:la\s+)?documentazione|informazioni)\s+'
    r'(?:(?:sul(?:la)?|sui|su|per|relativ[ae]\s+a)(?:\s+|$))'
    r'(?:(?:il|lo|la|i|le)\s+)?'
    r'(?:(?:function\s+module|modulo\s+funzione)\s+)?',
    re.IGNORECASE,
)
CONCEPT_STOP_WORDS = {'OF', 'AN', 'AND', 'THE', 'TO', 'FOR', 'A', 'IN'}


class ExplorerError(Exception):
    """Errore operativo presentabile nell'interfaccia."""


@lru_cache(maxsize=1)
def detector() -> Magika:
    return Magika()


def sanitize_query(query: str) -> str:
    cleaned = TOOL_TAG.sub('', query)
    quoted = QUOTED_TEXT.search(cleaned)
    if quoted:
        cleaned = next(group for group in quoted.groups() if group is not None)
    else:
        cleaned = FILLER.sub('', cleaned)
    cleaned = ' '.join(cleaned.split()).strip(' \"\'.,:;!?')
    if not cleaned:
        raise ExplorerError('La ricerca non contiene parole chiave dopo la sanificazione.')
    return cleaned


def query_candidates(query: str) -> list[str]:
    candidates = [query]
    if '_' in query:
        words = query.replace('_', ' ').split()
        conceptual = ' '.join(word for word in words if word.upper() not in CONCEPT_STOP_WORDS)
        if conceptual and conceptual.casefold() != query.casefold():
            candidates.append(conceptual)
    return candidates


def result_quality(result) -> tuple[float, int]:
    payload = result.structuredContent
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        payload = None
        for item in result.content:
            if item.type != 'text':
                continue
            try:
                parsed = json.loads(item.text)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get('results'), list):
                payload = parsed
                break
    items = payload['results'] if payload is not None else []
    scores = [
        item['score'] for item in items
        if isinstance(item, dict) and isinstance(item.get('score'), (int, float))
        and not isinstance(item['score'], bool) and math.isfinite(item['score'])
    ]
    return max(0.0, max(scores, default=0.0)), len(items)


@asynccontextmanager
async def connected_session(server_id: str | None = None, provider_factory: ProviderFactory | None = None):
    provider = (provider_factory or create_provider)(server_id)
    async with provider.connect() as session:
        yield session


def list_servers() -> dict:
    try:
        return {'servers': available_servers(), 'default_server': default_server_id()}
    except McpProviderError as error:
        raise ExplorerError(str(error)) from error


async def list_server_tools(server_id: str | None = None, provider_factory: ProviderFactory | None = None) -> dict:
    try:
        provider = (provider_factory or create_provider)(server_id)
        tools = await provider.discover_tools()
    except McpProviderError as error:
        raise ExplorerError(str(error)) from error
    except Exception as error:
        label = getattr(locals().get('provider'), 'label', 'selezionato')
        raise ExplorerError(f'Impossibile ottenere i tool dal server {label}.') from error
    if not provider.allow_dynamic_tools:
        tools = {name: tool for name, tool in tools.items() if name in TOOLS}
    return {
        'server_id': provider.server_id,
        'dynamic_arguments': provider.allow_dynamic_tools,
        'tools': [
            {
                'name': name, 'label': TOOLS.get(name, name),
                'description': tool.description or '', 'inputSchema': tool.inputSchema,
            }
            for name, tool in tools.items()
        ],
    }


def tool_arguments(tool_name: str, text: str, system_type: str = 'public_cloud') -> dict:
    if not isinstance(tool_name, str) or tool_name not in TOOLS:
        raise ExplorerError('Strumento MCP non supportato.')
    if not isinstance(text, str):
        raise ExplorerError('I parametri dello strumento devono essere testuali.')
    if not text.strip() and tool_name not in {'abap_feature_matrix', 'sap_search_objects'}:
        raise ExplorerError('Inserisci i parametri dello strumento selezionato.')
    if tool_name == 'abap_lint':
        if len(text.encode('utf-8')) > MAX_CODE_BYTES:
            raise ExplorerError('Il codice ABAP supera il limite di 50 KB UTF-8.')
        return {'code': text}
    if len(text) > 4000:
        raise ExplorerError('Inserisci al massimo 4000 caratteri.')
    if tool_name in {'sap_search_objects', 'sap_get_object_details'}:
        if not isinstance(system_type, str) or system_type not in {
            'public_cloud', 'btp', 'private_cloud', 'on_premise',
        }:
            raise ExplorerError('Sistema SAP non supportato.')
    else:
        system_type = None
    if tool_name == 'fetch':
        return {'id': text.strip()}
    if tool_name == 'sap_discovery_center_service':
        return {'serviceId': text.strip()}
    if tool_name == 'sap_get_object_details':
        parts = [part.strip() for part in text.split(',')]
        if len(parts) != 2 or not all(parts):
            raise ExplorerError('Inserisci object_type, object_name separati da una sola virgola.')
        quote_pairs = {'"': '"', "'": "'", '\u201c': '\u201d', '\u00ab': '\u00bb', '\u2018': '\u2019'}
        object_type, object_name = parts
        for opening, closing in quote_pairs.items():
            if object_name.startswith(opening) and object_name.endswith(closing):
                object_name = object_name[len(opening):-len(closing)].strip()
                break
        return {'object_type': object_type, 'object_name': object_name, 'system_type': system_type}
    if tool_name == 'sap_search_objects':
        return {'query': text.strip(), 'system_type': system_type}
    return {'query': text.strip()}


async def invoke_tool(
    tool_name: str, text: str, count: int = 3,
    system_type: str = 'public_cloud', server_id: str | None = None,
    provider_factory: ProviderFactory | None = None,
) -> dict:
    try:
        provider = (provider_factory or create_provider)(server_id)
        dynamic_arguments = provider.allow_dynamic_tools
        if dynamic_arguments:
            catalog = await provider.discover_tools()
            tool = catalog.get(tool_name)
            if tool is None:
                raise ExplorerError('Il tool selezionato non e disponibile sul server MCP scelto.')
            if not isinstance(text, str) or len(text.encode('utf-8')) > 320 * 1024:
                raise ExplorerError('Gli argomenti MCP devono essere testuali e non superare 320 KB UTF-8.')
            try:
                arguments = json.loads(text) if text.strip() else {}
            except (json.JSONDecodeError, TypeError) as error:
                raise ExplorerError('Per i tool On-Premise inserisci gli argomenti come oggetto JSON.') from error
            if (
                not isinstance(arguments, dict)
                or len(json.dumps(arguments, ensure_ascii=False).encode('utf-8')) > 320 * 1024
                or not Draft202012Validator(tool.inputSchema).is_valid(arguments)
            ):
                raise ExplorerError('Gli argomenti non rispettano lo schema MCP del tool selezionato.')
        else:
            arguments = tool_arguments(tool_name, text, system_type)
    except McpProviderError as error:
        raise ExplorerError(str(error)) from error
    except ExplorerError:
        raise
    except Exception as error:
        raise ExplorerError('Impossibile scoprire o validare i tool del server MCP selezionato.') from error
    if tool_name == 'search' and not dynamic_arguments:
        return await search(text, count, provider.server_id, provider_factory)
    try:
        result = await provider.execute_tool(name=tool_name, arguments=arguments)
        raw = '\n\n'.join(item.text for item in result.content if item.type == 'text')
        if result.isError:
            raise ExplorerError(f'Errore del tool {tool_name}: ' + (raw[:2000] or 'nessun dettaglio.'))
        snapshot = {
            'tool': tool_name, 'arguments': arguments, 'query': text,
            'original_query': text, 'endpoint': getattr(provider, 'endpoint', f'stdio:{provider.server_id}'),
            'server_id': provider.server_id,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'raw_text': raw, 'response': result.model_dump(mode='json'),
            'attempts': [], 'search_log': [f'Strumento invocato: {tool_name}'],
        }
        if len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8')) > MAX_CONTENT:
            raise ExplorerError('La risposta supera il limite di 5 MB.')
        return snapshot
    except ExplorerError:
        raise
    except TimeoutError as error:
        raise ExplorerError('Tempo massimo di attesa superato. Riprova tra poco.') from error
    except Exception as error:
        if isinstance(error, McpProviderError):
            raise ExplorerError(str(error)) from error
        raise ExplorerError(f'Chiamata MCP {tool_name} non riuscita. Verifica rete e disponibilita del server.') from error


async def search(
    query: str, count: int = 3, server_id: str | None = None,
    provider_factory: ProviderFactory | None = None,
) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        raise ExplorerError('Inserisci una ricerca da 1 a 4000 caratteri.')
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
        raise ExplorerError('Il numero di risultati deve essere compreso tra 1 e 20.')
    original_query = query.strip()
    optimized_query = sanitize_query(original_query)
    attempts = []
    best_result = None
    best_quality = (-math.inf, False)
    selected_query = optimized_query
    search_log = [f'Query ottimizzata in: {optimized_query}']
    try:
        async with connected_session(server_id, provider_factory) as session:
            for candidate in query_candidates(optimized_query):
                result = await session.call_tool(name='search', arguments={'query': candidate, 'k': count})
                raw = '\n\n'.join(item.text for item in result.content if item.type == 'text')
                if result.isError:
                    raise ExplorerError('Errore del tool search: ' + (raw[:2000] or 'nessun dettaglio.'))
                score, result_count = result_quality(result)
                attempts.append({'query': candidate, 'score': score, 'result_count': result_count})
                quality = (score, bool(result_count))
                if best_result is None or quality > best_quality:
                    best_result = result
                    best_quality = quality
                    selected_query = candidate
                if result_count and score >= MIN_SCORE:
                    break
        result = best_result
        raw = '\n\n'.join(item.text for item in result.content if item.type == 'text')
        if len(attempts) > 1:
            search_log.append('Applicato fallback automatico per punteggio basso o risultati vuoti.')
        search_log.extend(
            f"Tentativo {index}: {attempt['query']} / score {attempt['score']:.4f} / {attempt['result_count']} risultati"
            for index, attempt in enumerate(attempts, start=1)
        )
        search_log.append(f'Query selezionata: {selected_query} / score {best_quality[0]:.4f}')
        if not best_quality[1] or best_quality[0] < MIN_SCORE:
            search_log.append('Nessun tentativo ha raggiunto la soglia 0.06; mostrata la migliore risposta disponibile.')
        snapshot = {
            'tool': 'search', 'arguments': {'query': selected_query, 'k': count},
            'query': selected_query, 'original_query': original_query,
            'optimized_query': optimized_query, 'attempts': attempts,
            'search_log': search_log, 'score_threshold': MIN_SCORE,
            'count': count, 'endpoint': getattr(
                (provider_factory or create_provider)(server_id), 'endpoint',
                f'stdio:{server_id or default_server_id()}',
            ),
            'server_id': server_id or default_server_id(),
            'created_at': datetime.now(timezone.utc).isoformat(),
            'raw_text': raw, 'response': result.model_dump(mode='json'),
        }
        if len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8')) > MAX_CONTENT:
            raise ExplorerError('La risposta supera il limite di 5 MB.')
        return snapshot
    except ExplorerError:
        raise
    except TimeoutError as error:
        raise ExplorerError('Tempo massimo di attesa superato. Riprova tra poco.') from error
    except Exception as error:
        if isinstance(error, McpProviderError):
            raise ExplorerError(str(error)) from error
        raise ExplorerError('Connessione MCP non riuscita. Verifica rete e disponibilita del server.') from error


def format_response(snapshot: dict, format_name: str) -> bytes:
    if format_name == 'json':
        text = json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n'
    elif format_name == 'txt':
        text = snapshot['raw_text']
    elif format_name == 'md':
        raw = snapshot['raw_text']
        if not raw:
            raw = json.dumps(snapshot['response'], ensure_ascii=False, indent=2)
        fence = '~~~~'
        while fence in raw:
            fence += '~'
        text = (
            '# Risposta MCP SAP ABAP\n\n'
            f"- Strumento: {snapshot.get('tool', 'search')}\n"
            f"- Query: {snapshot['query'].replace(chr(10), ' ')}\n"
            f"- Endpoint: {snapshot['endpoint']}\n"
            f"- Data UTC: {snapshot['created_at']}\n\n"
            f'## Contenuto restituito\n\n{fence}text\n{raw}\n{fence}\n'
        )
    else:
        raise ExplorerError('Formato non supportato. Scegli JSON, TXT o Markdown.')
    payload = text.encode('utf-8')
    if not payload:
        raise ExplorerError('Il server non ha restituito content.text. Esporta la risposta in JSON o Markdown.')
    if len(payload) > MAX_CONTENT:
        raise ExplorerError('Il file supera il limite di 5 MB.')
    return payload


def export_response(snapshot: dict, format_name: str, directory: Path) -> dict:
    payload = format_response(snapshot, format_name)
    # Magika analizza gli stessi byte che saranno scritti, prima di creare il file.
    with MAGIKA_LOCK:
        identification = detector().identify_bytes(payload)
    output = identification.output
    if output is None or not output.is_text:
        raise ExplorerError('Magika non ha riconosciuto contenuto testuale: salvataggio annullato.')
    if format_name == 'json':
        json.loads(payload)
    filename = f"mcp_abap_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid4().hex[:12]}.{format_name}"
    destination = directory / filename
    try:
        with destination.open('xb') as stream:
            stream.write(payload)
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise ExplorerError('Impossibile salvare il file nella cartella dell\'applicazione.') from error
    return {
        'filename': filename, 'path': str(destination.resolve()),
        'mime_type': output.mime_type, 'label': output.label, 'bytes': len(payload),
    }