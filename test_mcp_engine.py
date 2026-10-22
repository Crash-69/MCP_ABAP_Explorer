import json
import os
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import httpx
from mcp.types import CallToolResult, TextContent

import app
import llm_engine
import mcp_providers
import sap_diagnostics
from mcp_engine import ExplorerError, export_response, format_response, invoke_tool, query_candidates, result_quality, sanitize_query, search, tool_arguments


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            'query': 'SELECT in ABAP', 'endpoint': 'https://example.test/mcp',
            'created_at': '2026-10-19T12:00:00+00:00',
            'raw_text': 'Risposta SAP: Unicode \u00e8 supportato.\n~~~~\nSELECT * FROM demo.',
            'response': {'content': [{'type': 'text', 'text': 'Risposta SAP'}]},
        }

    def test_formats_preserve_unicode_and_response(self):
        self.assertEqual(json.loads(format_response(self.snapshot, 'json')), self.snapshot)
        self.assertEqual(format_response(self.snapshot, 'txt').decode(), self.snapshot['raw_text'])
        markdown = format_response(self.snapshot, 'md').decode()
        self.assertIn('~~~~~text\n', markdown)
        self.assertIn(self.snapshot['raw_text'], markdown)
        with self.assertRaises(ExplorerError):
            format_response(self.snapshot, 'exe')

    def test_real_magika_and_three_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for extension in ('json', 'txt', 'md'):
                result = export_response(self.snapshot, extension, directory)
                self.assertTrue(result['mime_type'])
                self.assertEqual((directory / result['filename']).read_bytes(), format_response(self.snapshot, extension))
            self.assertEqual(len(list(directory.iterdir())), 3)

    def test_failed_magika_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('mcp_engine.detector') as factory:
                factory.return_value.identify_bytes.return_value = SimpleNamespace(output=SimpleNamespace(is_text=False))
                with self.assertRaises(ExplorerError):
                    export_response(self.snapshot, 'txt', Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_invalid_search_without_network(self):
        for query, count in (('', 3), ('ABAP', 0), ('ABAP', True), ('ABAP', 21)):
            with self.assertRaises(ExplorerError):
                anyio.run(search, query, count)

    def test_missing_text_still_exports_json_and_markdown(self):
        self.snapshot['raw_text'] = ''
        with self.assertRaises(ExplorerError):
            format_response(self.snapshot, 'txt')
        self.assertTrue(format_response(self.snapshot, 'json'))
        self.assertTrue(format_response(self.snapshot, 'md'))


class SearchTests(unittest.TestCase):
    def result(self, scores, structured=True):
        payload = {'results': [{'score': score, 'title': 'SAP'} for score in scores]}
        return CallToolResult(
            content=[TextContent(type='text', text=json.dumps(payload))],
            structuredContent=payload if structured else None,
        )

    def run_search(self, results, query='@search Cerca la documentazione sul function module "TR_GET_OBJECTS_OF_REQ_AN_TASKS"'):
        call_tool = AsyncMock(side_effect=results)

        @asynccontextmanager
        async def session(*args, **kwargs):
            yield SimpleNamespace(initialize=AsyncMock(), call_tool=call_tool)

        with patch('mcp_engine.connected_session', session):
            snapshot = anyio.run(search, query, 3)
        return snapshot, call_tool

    def test_sanitization(self):
        for query in (
            '@search Cerca la documentazione sul function module "TR_GET_OBJECTS_OF_REQ_AN_TASKS"',
            '@fetch Cerca documentazione per TR_GET_OBJECTS_OF_REQ_AN_TASKS',
        ):
            self.assertEqual(sanitize_query(query), 'TR_GET_OBJECTS_OF_REQ_AN_TASKS')
        for quotes in (('"', '"'), ("'", "'"), ('\u201c', '\u201d'), ('\u00ab', '\u00bb')):
            self.assertEqual(sanitize_query(f'Trova informazioni su {quotes[0]}E071{quotes[1]}'), 'E071')
        self.assertEqual(sanitize_query('SELECT statements in ABAP Cloud'), 'SELECT statements in ABAP Cloud')
        for empty in ('@search', '@fetch " "', 'Cerca documentazione per'):
            with self.assertRaises(ExplorerError):
                sanitize_query(empty)

    def test_candidates_only_transform_input(self):
        self.assertEqual(query_candidates('TR_GET_OBJECTS_OF_REQ_AN_TASKS'), [
            'TR_GET_OBJECTS_OF_REQ_AN_TASKS', 'TR GET OBJECTS REQ TASKS',
        ])
        self.assertEqual(query_candidates('BAPI_EMPLOYEE_GETDATA'), [
            'BAPI_EMPLOYEE_GETDATA', 'BAPI EMPLOYEE GETDATA',
        ])
        self.assertEqual(query_candidates('SELECT statements in ABAP Cloud'), ['SELECT statements in ABAP Cloud'])
        for table in ('E070', 'E071', 'BKPF', 'Z_ANY_TABLE'):
            candidates = query_candidates(f'Table {table}')
            self.assertEqual(candidates[0], f'Table {table}')
            self.assertTrue(all(set(candidate.split()) <= set(f'Table {table}'.replace('_', ' ').split()) for candidate in candidates[1:]))

    def test_no_context_mapping_or_fixed_prefixes(self):
        self.assertEqual(query_candidates('R_GET_OBJECTS_OF_REQ_AN_TASKS'), [
            'R_GET_OBJECTS_OF_REQ_AN_TASKS', 'R GET OBJECTS REQ TASKS',
        ])
        self.assertEqual(query_candidates('Z_CUSTOM_GET_DATA'), ['Z_CUSTOM_GET_DATA', 'Z CUSTOM GET DATA'])
        for query in ('SAP repository objects', 'richieste di trasporto', 'function module directory', 'request tasks'):
            self.assertEqual(query_candidates(query), [query])
        snapshot, call = self.run_search([self.result([0.01]), self.result([0.07])], '@fetch Trova informazioni su "Z_CUSTOM_GET_DATA"')
        self.assertEqual(call.await_count, 2)
        self.assertEqual(snapshot['optimized_query'], 'Z_CUSTOM_GET_DATA')
        self.assertEqual(snapshot['query'], 'Z CUSTOM GET DATA')

    def test_invalid_scores_do_not_pass_threshold(self):
        self.assertEqual(result_quality(self.result([None, True, float('nan'), float('inf'), '0.99', -0.1])), (0.0, 6))

    def test_threshold_stops_without_fallback(self):
        snapshot, call = self.run_search([self.result([0.01, 0.06])])
        self.assertEqual(call.await_count, 1)
        self.assertEqual(snapshot['query'], 'TR_GET_OBJECTS_OF_REQ_AN_TASKS')
        self.assertEqual(snapshot['attempts'][0]['score'], 0.06)

    def test_conceptual_fallback(self):
        snapshot, call = self.run_search([self.result([0.02]), self.result([0.08], structured=False)])
        self.assertEqual(call.await_count, 2)
        self.assertEqual(snapshot['query'], 'TR GET OBJECTS REQ TASKS')
        self.assertIn('fallback automatico', ' '.join(snapshot['search_log']))

    def test_empty_then_dynamic_fallback(self):
        snapshot, call = self.run_search([self.result([]), self.result([0.03])])
        self.assertEqual(call.await_count, 2)
        self.assertEqual(snapshot['query'], 'TR GET OBJECTS REQ TASKS')
        self.assertEqual(json.loads(snapshot['raw_text'])['results'][0]['score'], 0.03)
        self.assertEqual(snapshot['response']['structuredContent']['results'][0]['score'], 0.03)
        self.assertEqual([entry.kwargs['arguments']['query'] for entry in call.await_args_list], [
            'TR_GET_OBJECTS_OF_REQ_AN_TASKS', 'TR GET OBJECTS REQ TASKS',
        ])

    def test_all_low_keep_best_and_stable_ties(self):
        for fallback_score in (0.03, 0.05):
            snapshot, _ = self.run_search([self.result([0.05]), self.result([fallback_score])])
            self.assertEqual(snapshot['query'], 'TR_GET_OBJECTS_OF_REQ_AN_TASKS')
            self.assertIn('Nessun tentativo', snapshot['search_log'][-1])

    def test_all_empty_and_missing_scores(self):
        snapshot, call = self.run_search([self.result([]), self.result([])])
        self.assertEqual(call.await_count, 2)
        self.assertEqual(snapshot['query'], 'TR_GET_OBJECTS_OF_REQ_AN_TASKS')
        snapshot, _ = self.run_search([self.result([None]), self.result([0.04])])
        self.assertEqual(snapshot['query'], 'TR GET OBJECTS REQ TASKS')

    def test_different_inputs_reach_mcp_without_added_terms(self):
        for index in range(3):
            name = f'Z_{index}_FETCH_DETAILS'
            snapshot, call = self.run_search(
                [self.result([]), self.result([0.01])],
                f'@search Cerca la documentazione sul function module "{name}"',
            )
            self.assertEqual([entry.kwargs['arguments']['query'] for entry in call.await_args_list], [
                name, name.replace('_', ' '),
            ])
            self.assertEqual(snapshot['optimized_query'], name)
            self.assertEqual(len(snapshot['attempts']), 2)

    def test_tool_error_is_not_a_low_score(self):
        error = CallToolResult(content=[TextContent(type='text', text='Errore SAP')], isError=True)
        with self.assertRaisesRegex(ExplorerError, 'Errore del tool search'):
            self.run_search([error])


class ToolTests(unittest.TestCase):
    def test_argument_mapping(self):
        for tool in ('search', 'sap_community_search', 'sap_search_objects', 'abap_feature_matrix', 'sap_discovery_center_search'):
            expected = {'query': 'SAP topic'}
            if tool == 'sap_search_objects':
                expected['system_type'] = 'public_cloud'
            self.assertEqual(tool_arguments(tool, '  SAP topic  '), expected)
        for tool in ('abap_feature_matrix', 'sap_search_objects'):
            expected = {'query': ''}
            if tool == 'sap_search_objects':
                expected['system_type'] = 'public_cloud'
            self.assertEqual(tool_arguments(tool, ''), expected)
        self.assertEqual(tool_arguments('fetch', ' /library/document_id '), {'id': '/library/document_id'})
        self.assertEqual(tool_arguments('sap_discovery_center_service', ' service-uuid '), {'serviceId': 'service-uuid'})
        code = '\nREPORT z_demo.\nWRITE: / "quoted", / \'@search\'.\n'
        self.assertEqual(tool_arguments('abap_lint', code), {'code': code})
        self.assertEqual(tool_arguments('sap_get_object_details', ' CLAS, CL_ABAP_REGEX '), {
            'object_type': 'CLAS', 'object_name': 'CL_ABAP_REGEX', 'system_type': 'public_cloud',
        })
        self.assertEqual(tool_arguments('sap_get_object_details', 'FUNC, "BAPI_EMPLOYEE_GETLIST"', 'on_premise'), {
            'object_type': 'FUNC', 'object_name': 'BAPI_EMPLOYEE_GETLIST', 'system_type': 'on_premise',
        })
        self.assertEqual(tool_arguments('sap_search_objects', 'BAPI_EMPLOYEE_GETLIST', 'on_premise'), {
            'query': 'BAPI_EMPLOYEE_GETLIST', 'system_type': 'on_premise',
        })

    def test_validation_before_network(self):
        for tool, text in (
            ('unknown', 'SAP'), ('fetch', ' '), ('sap_search_objects', 'a' * 4001),
            ('sap_get_object_details', 'CLAS'), ('sap_get_object_details', 'CLAS,'),
            ('sap_get_object_details', 'CLAS,NAME,EXTRA'), ('abap_lint', '\u00e8' * 25601),
            ('sap_discovery_center_service', ''), ('sap_discovery_center_search', ''),
        ):
            with self.subTest(tool=tool), self.assertRaises(ExplorerError):
                anyio.run(invoke_tool, tool, text)
        with self.assertRaises(ExplorerError):
            tool_arguments('sap_get_object_details', 'FUNC, BAPI_EMPLOYEE_GETLIST', 'invalid')
        with self.assertRaises(ExplorerError):
            tool_arguments('sap_get_object_details', 'FUNC, BAPI_EMPLOYEE_GETLIST', [])

    def test_search_dispatch_preserves_cascade(self):
        with patch('mcp_engine.search', new=AsyncMock(return_value={'tool': 'search'})) as mocked:
            self.assertEqual(anyio.run(invoke_tool, 'search', '@search topic', 7), {'tool': 'search'})
            mocked.assert_awaited_once_with('@search topic', 7, 'sap-cloud', None)

    def test_non_search_calls_and_exports(self):
        cases = (
            ('fetch', '/library/doc_id', {'id': '/library/doc_id'}),
            ('abap_lint', 'REPORT z_demo.\nWRITE \'@search\'.\n', {'code': 'REPORT z_demo.\nWRITE \'@search\'.\n'}),
            ('sap_community_search', 'SELECT performance', {'query': 'SELECT performance'}),
            ('sap_search_objects', 'CL_ABAP_REGEX', {'query': 'CL_ABAP_REGEX', 'system_type': 'public_cloud'}),
            ('sap_get_object_details', 'CLAS, CL_ABAP_REGEX', {
                'object_type': 'CLAS', 'object_name': 'CL_ABAP_REGEX', 'system_type': 'public_cloud',
            }),
            ('abap_feature_matrix', 'inline declaration', {'query': 'inline declaration'}),
            ('abap_feature_matrix', '', {'query': ''}),
            ('sap_discovery_center_search', 'SAP Build', {'query': 'SAP Build'}),
            ('sap_discovery_center_service', 'service-uuid', {'serviceId': 'service-uuid'}),
        )
        result = CallToolResult(content=[TextContent(type='text', text='SAP tool response\nDettagli del risultato.')])
        for tool, text, expected in cases:
            call_tool = AsyncMock(return_value=result)
            provider = SimpleNamespace(
                server_id='sap-cloud', endpoint='https://example.test/mcp', allow_dynamic_tools=False,
                execute_tool=call_tool,
            )
            with self.subTest(tool=tool), patch('mcp_engine.create_provider', return_value=provider):
                snapshot = anyio.run(invoke_tool, tool, text, 9)
                call_tool.assert_awaited_once_with(name=tool, arguments=expected)
                self.assertEqual(snapshot['tool'], tool)
                self.assertEqual(snapshot['arguments'], expected)
                self.assertEqual(snapshot['raw_text'], result.content[0].text)
                with tempfile.TemporaryDirectory() as directory:
                    for extension in ('json', 'txt', 'md'):
                        exported = export_response(snapshot, extension, Path(directory))
                        self.assertTrue(Path(exported['path']).is_file())
                        self.assertTrue(exported['mime_type'])

    def test_on_premise_tool_is_discovered_and_schema_validated(self):
        tool = SimpleNamespace(inputSchema={
            'type': 'object', 'properties': {'table_name': {'type': 'string'}},
            'required': ['table_name'], 'additionalProperties': False,
        })
        result = CallToolResult(content=[TextContent(type='text', text='Metadati runtime SAP')])
        provider = SimpleNamespace(
            server_id='sap-on-premise', allow_dynamic_tools=True,
            discover_tools=AsyncMock(return_value={'adt_get_table': tool}),
            execute_tool=AsyncMock(return_value=result),
        )
        with patch('mcp_engine.create_provider', return_value=provider):
            snapshot = anyio.run(
                invoke_tool, 'adt_get_table', '{"table_name":"T000"}', 3,
                'public_cloud', 'sap-on-premise',
            )
            self.assertEqual(snapshot['arguments'], {'table_name': 'T000'})
            provider.execute_tool.assert_awaited_once_with(name='adt_get_table', arguments={'table_name': 'T000'})
            with self.assertRaisesRegex(ExplorerError, 'schema'):
                anyio.run(invoke_tool, 'adt_get_table', '{"wrong":"T000"}', 3, 'public_cloud', 'sap-on-premise')
            with self.assertRaisesRegex(ExplorerError, 'non e disponibile'):
                anyio.run(invoke_tool, 'sap_get_object_details', '{}', 3, 'public_cloud', 'sap-on-premise')


class ProviderTests(unittest.TestCase):
    def test_adt_config_uses_package_environment_contract(self):
        on_premise = mcp_providers._load_config()['servers']['sap-on-premise']
        self.assertEqual(set(on_premise['env']), {
            'SAP_URL', 'SAP_USERNAME', 'SAP_PASSWORD', 'SAP_CLIENT', 'SAP_SYSNR', 'SAP_SYSTEM_ID',
        })
        self.assertNotIn('SAP_ASHOST', on_premise['env'])
        self.assertNotIn('SAP_USER', on_premise['env'])

    def test_provider_factory_uses_external_config_and_environment(self):
        with patch.dict(os.environ, {
            'SAP_URL': 'https://sap.internal', 'SAP_CLIENT': '100',
            'SAP_USERNAME': 'runtime-user', 'SAP_PASSWORD': 'runtime-secret',
        }):
            cloud = mcp_providers.create_provider('sap-cloud')
            on_premise = mcp_providers.create_provider('sap-on-premise', {
                'SAP_URL': 'https://vhsrqds4ci.sap.campania.local', 'SAP_CLIENT': '100',
                'SAP_SYSNR': '00', 'SAP_SYSTEM_ID': 'DS4',
                'SAP_USERNAME': 'BCECCARELLI', 'SAP_PASSWORD': 'temporary-test-password',
            })
        self.assertIsInstance(cloud, mcp_providers.CloudMcpProvider)
        self.assertEqual(cloud.endpoint, mcp_providers.resolve_value('${SAP_CLOUD_MCP_ENDPOINT:-https://mcp-abap.marianzeis.de/mcp}'))
        self.assertIsInstance(on_premise, mcp_providers.OnPremiseMcpProvider)
        self.assertEqual(on_premise.env['SAP_URL'], 'https://vhsrqds4ci.sap.campania.local')
        self.assertEqual(on_premise.env['SAP_SYSNR'], '00')
        self.assertEqual(on_premise.env['SAP_SYSTEM_ID'], 'DS4')
        self.assertEqual(on_premise.env['SAP_CLIENT'], '100')
        self.assertEqual(on_premise.env['SAP_USERNAME'], 'BCECCARELLI')
        self.assertEqual(on_premise.env['SAP_PASSWORD'], 'temporary-test-password')

    def test_provider_factory_rejects_missing_or_hardcoded_secrets(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(mcp_providers.McpProviderError, 'SAP_USERNAME'):
                mcp_providers.create_provider('sap-on-premise')
        with patch('mcp_providers._load_config', return_value={
            'servers': {'bad': {
                'label': 'bad', 'transport': 'stdio', 'command': 'npx',
                'env': {'SAP_PASSWORD': 'do-not-store-this'},
            }},
        }):
            with self.assertRaisesRegex(mcp_providers.McpProviderError, 'variabile di ambiente'):
                mcp_providers.create_provider('bad')
        with patch('mcp_providers._load_config', return_value={
            'servers': {'bad': {
                'label': 'bad', 'transport': 'stdio', 'command': 'npx',
                'env': {'SAP_PASSWORD': '${SAP_PASSWORD:-hardcoded-fallback}'},
            }},
        }):
            with self.assertRaisesRegex(mcp_providers.McpProviderError, 'senza fallback'):
                mcp_providers.create_provider('bad')

    def test_stdio_error_output_redacts_credentials(self):
        writer = mcp_providers.SafeErrorWriter(['runtime-secret'])
        self.assertGreaterEqual(writer.fileno(), 0)
        os.write(writer.fileno(), b'ADT login failed for runtime-secret')
        writer.close()
        self.assertEqual(writer.message(), 'ADT login failed for [REDACTED]')


class DiagnosticsTests(unittest.TestCase):
    def run_diagnostics(
        self, http_status=200, tools=('read_table', 'get_class'),
        ssl_failure=False, startup_error=None, discovery_error=None,
        host_unreachable=False, connect_timeout=False,
    ):
        requests = []
        async def handle(request):
            requests.append(request)
            if ssl_failure:
                raise httpx.ConnectError('TLS handshake failed', request=request) from ssl.SSLError('certificate verify failed')
            if connect_timeout:
                raise httpx.ConnectTimeout('connect timed out', request=request)
            return httpx.Response(http_status, request=request)

        @asynccontextmanager
        async def connect():
            if startup_error:
                raise mcp_providers.McpProviderError(startup_error)
            if discovery_error:
                session = SimpleNamespace(list_tools=AsyncMock(side_effect=RuntimeError(discovery_error)))
            else:
                session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(
                    tools=[SimpleNamespace(name=name) for name in tools], nextCursor=None,
                )))
            yield session

        provider = SimpleNamespace(connect=connect)
        def factory(server_id, options):
            self.assertEqual(server_id, 'sap-on-premise')
            self.assertEqual(options['SAP_USERNAME'], 'BCECCARELLI')
            self.assertEqual(options['SAP_PASSWORD'], 'temporary-secret')
            self.assertEqual(options['SAP_SYSNR'], '00')
            self.assertEqual(options['SAP_SYSTEM_ID'], 'DS4')
            return provider

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)

        connection_options = {
            'SAP_URL': 'https://sap.example.local', 'SAP_USERNAME': 'BCECCARELLI',
            'SAP_PASSWORD': 'temporary-secret', 'SAP_CLIENT': '100',
            'SAP_SYSNR': '00', 'SAP_SYSTEM_ID': 'DS4',
        }

        async def execute():
            with patch('sap_diagnostics._check_network', new=AsyncMock(return_value=(
                {'status': 'OK', 'addressesFound': 1},
                {'status': 'ERROR', 'error': 'TimeoutError'} if host_unreachable else {'status': 'OK'},
            ))):
                report = await sap_diagnostics.diagnose_sap_onpremise(
                    connection_options, factory, client_factory,
                )
                return report
        report = anyio.run(execute)
        return report, requests

    def test_http_status_classification_and_tools(self):
        with self.assertLogs('mcp.diagnostics', level='INFO') as captured:
            report, requests = self.run_diagnostics(200)
        self.assertEqual(report['adt']['status'], 'OK')
        self.assertEqual(report['authentication']['status'], 'OK')
        self.assertEqual(report['adtPermissions']['status'], 'OK')
        self.assertEqual(report['toolDiscovery']['tools'], ['get_class', 'read_table'])
        self.assertEqual(report['toolsFound'], 2)
        self.assertEqual(requests[0].url.path, '/sap/bc/adt')
        self.assertEqual(requests[0].url.params['sap-client'], '100')
        self.assertNotIn('temporary-secret', json.dumps(report))
        self.assertNotIn('BCECCARELLI', json.dumps(report))
        self.assertNotIn('temporary-secret', '\n'.join(captured.output))
        self.assertNotIn('BCECCARELLI', '\n'.join(captured.output))
        self.assertEqual(self.run_diagnostics(401)[0]['authentication']['status'], 'ERROR')
        forbidden = self.run_diagnostics(403)[0]
        self.assertEqual(forbidden['adtPermissions']['status'], 'ERROR')
        missing = self.run_diagnostics(404)[0]
        self.assertEqual(missing['adt']['status'], 'NOT_FOUND')
        self.assertTrue(any('Attivare' in text for text in missing['suggestions']))
        no_tools = self.run_diagnostics(200, tools=())[0]
        self.assertEqual(no_tools['mcpStartup']['status'], 'OK')
        self.assertEqual(no_tools['toolDiscovery']['status'], 'ERROR')

    def test_mcp_discovery_success_with_unreachable_sap_is_partial(self):
        tool_names = tuple(f'tool_{index}' for index in range(16))
        report, _ = self.run_diagnostics(
            tools=tool_names, host_unreachable=True, connect_timeout=True,
        )
        self.assertEqual(report['mcpStartup']['status'], 'OK')
        self.assertEqual(report['toolDiscovery']['status'], 'OK')
        self.assertEqual(report['toolsFound'], 16)
        self.assertEqual(report['adt']['status'], 'UNREACHABLE')
        self.assertEqual(report['summary'], 'PARTIAL')
        self.assertIn('MCP operativo', report['summaryMessage'])

    def test_ssl_and_mcp_startup_discovery_failures(self):
        ssl_report = self.run_diagnostics(ssl_failure=True)[0]
        self.assertEqual(ssl_report['adt']['status'], 'SSL_ERROR')
        startup_report = self.run_diagnostics(startup_error='npx missing')[0]
        self.assertEqual(startup_report['mcpStartup']['status'], 'ERROR')
        self.assertEqual(startup_report['toolDiscovery']['status'], 'ERROR')
        discovery_report = self.run_diagnostics(discovery_error='list_tools failed')[0]
        self.assertEqual(discovery_report['mcpStartup']['status'], 'OK')
        self.assertEqual(discovery_report['toolDiscovery']['status'], 'ERROR')

    def test_missing_env_skips_network_and_mcp(self):
        async def execute():
            return await sap_diagnostics.diagnose_sap_onpremise({
                'SAP_URL': 'https://sap.example.local', 'SAP_CLIENT': '100',
            })
        report = anyio.run(execute)
        self.assertEqual(report['environment']['status'], 'ERROR')
        self.assertEqual(report['authentication']['status'], 'SKIPPED')
        self.assertEqual(report['toolDiscovery']['status'], 'SKIPPED')

    def test_http_url_is_rejected_before_network_or_mcp(self):
        options = {
            'SAP_URL': 'http://sap.example.local', 'SAP_USERNAME': 'BCECCARELLI',
            'SAP_PASSWORD': 'temporary-secret', 'SAP_CLIENT': '100',
        }
        factory = AsyncMock()
        async def execute():
            with patch('sap_diagnostics._check_network', new=AsyncMock()) as network:
                report = await sap_diagnostics.diagnose_sap_onpremise(options, factory)
                network.assert_not_awaited()
                return report
        report = anyio.run(execute)
        factory.assert_not_awaited()
        self.assertEqual(report['adt']['status'], 'INSECURE_URL')
        self.assertEqual(report['mcpStartup']['status'], 'SKIPPED')
        self.assertNotIn('temporary-secret', json.dumps(report))

    def test_tool_error_reports_selected_name(self):
        provider = SimpleNamespace(
            server_id='sap-cloud', endpoint='https://example.test/mcp', allow_dynamic_tools=False,
            execute_tool=AsyncMock(return_value=CallToolResult(
                content=[TextContent(type='text', text='ID sconosciuto')], isError=True,
            )),
        )
        with patch('mcp_engine.create_provider', return_value=provider):
            with self.assertRaisesRegex(ExplorerError, 'Errore del tool fetch: ID sconosciuto'):
                anyio.run(invoke_tool, 'fetch', '/missing')


class LocalLLMTests(unittest.TestCase):
    def test_local_catalog_excludes_incompatible_and_cloud(self):
        async def verify():
            def handle(request):
                if request.url.path == '/api/tags':
                    return httpx.Response(200, json={'models': [
                        {'name': 'local', 'size': 10}, {'name': 'no-tools'},
                        {'name': 'remote', 'remote_host': 'https://cloud.test'},
                        {'name': 'large:cloud'}, {'name': 'hidden-remote'},
                    ]})
                name = json.loads(request.content)['model']
                return httpx.Response(200, json={
                    'capabilities': ['completion', 'tools'] if name != 'no-tools' else ['completion'],
                    **({'remote_model': 'cloud-model'} if name == 'hidden-remote' else {}),
                })
            async with httpx.AsyncClient(base_url=llm_engine.OLLAMA_URL, transport=httpx.MockTransport(handle)) as client:
                models = await llm_engine.local_model_catalog(client)
            self.assertEqual([item['name'] for item in models], ['local'])
        anyio.run(verify)

    def test_tool_schema_validation(self):
        catalog = {'fetch': SimpleNamespace(inputSchema={
            'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id'],
        })}
        llm_engine.validate_call('fetch', {'id': '/doc'}, catalog)
        for name, arguments in (('unknown', {}), ('fetch', {}), ('fetch', {'id': 7}), ('fetch', {'id': '/doc', 'extra': 1})):
            with self.assertRaises(ExplorerError):
                llm_engine.validate_call(name, arguments, catalog)

    def test_dynamic_tool_schema_is_authoritative(self):
        catalog = {'adt_get_table': SimpleNamespace(inputSchema={
            'type': 'object', 'properties': {'table_name': {'type': 'string'}},
            'required': ['table_name'], 'additionalProperties': True,
        })}
        llm_engine.validate_call(
            'adt_get_table', {'table_name': 'T000', 'language': 'E'}, catalog,
            strict_properties=False,
        )
        with self.assertRaises(ExplorerError):
            llm_engine.validate_call(
                'adt_get_table', {'table_name': 12}, catalog, strict_properties=False,
            )

    def test_compact_schema_preserves_constraints(self):
        original = {
            'type': 'object', 'description': 'Long documentation',
            'properties': {'description': {'type': 'string', 'description': 'Parameter docs'},
                           'kind': {'type': 'string', 'enum': ['A', 'B']}},
            'required': ['kind'],
        }
        compact = llm_engine.compact_schema(original)
        self.assertNotIn('description', compact)
        self.assertEqual(compact['properties']['description'], {'type': 'string'})
        self.assertEqual(compact['properties']['kind']['enum'], ['A', 'B'])
        self.assertEqual(compact['required'], ['kind'])

    def run_agent(self, replies, server_id='sap-cloud', dynamic=False):
        tool_name = 'adt_get_table' if dynamic else 'fetch'
        input_schema = {
            'type': 'object', 'properties': {'table_name': {'type': 'string'}}, 'required': ['table_name'],
        } if dynamic else {
            'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id'],
        }
        catalog = {tool_name: SimpleNamespace(name=tool_name, description='Read SAP object', inputSchema=input_schema)}
        call_tool = AsyncMock(return_value=CallToolResult(content=[TextContent(type='text', text='Documento SAP completo')]))

        provider = SimpleNamespace(
            server_id=server_id, endpoint='https://example.test/mcp' if not dynamic else None,
            allow_dynamic_tools=dynamic,
            execute_tool=call_tool,
        )

        chat = iter(replies)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'message': next(chat)})

        factory = httpx.AsyncClient
        with patch('llm_engine.httpx.AsyncClient', side_effect=lambda **kwargs: factory(**kwargs, transport=httpx.MockTransport(handle))):
            with patch('llm_engine.local_model_catalog', new=AsyncMock(return_value=[{'name': 'local', 'capabilities': ['completion', 'tools']}])):
                with patch('llm_engine.mcp_catalog', new=AsyncMock(return_value=catalog)), \
                    patch('llm_engine.create_provider', return_value=provider):
                    snapshot = anyio.run(llm_engine.run_local_llm, 'local', 'Leggi /doc', server_id)
        return snapshot, call_tool, requests

    def test_agent_calls_mcp_and_summarizes_with_exports(self):
        snapshot, call, requests = self.run_agent([
            {'content': '', 'tool_calls': [{'function': {'name': 'fetch', 'arguments': {'id': '/doc'}}}]},
            {'content': 'Sintesi basata sul documento SAP /doc.'},
        ])
        call.assert_awaited_once_with(name='fetch', arguments={'id': '/doc'})
        self.assertEqual(snapshot['mode'], 'llm')
        self.assertIn('Documento SAP completo', snapshot['mcp_raw_text'])
        self.assertIn('Sintesi', snapshot['raw_text'])
        self.assertEqual(requests[1]['messages'][-1]['role'], 'tool')
        self.assertIn('Documento SAP completo', requests[1]['messages'][-1]['content'])
        for extension in ('json', 'txt', 'md'):
            self.assertTrue(format_response(snapshot, extension))

    def test_on_premise_llm_uses_only_live_server_tools(self):
        snapshot, call, requests = self.run_agent([
            {'content': '', 'tool_calls': [{'function': {
                'name': 'adt_get_table', 'arguments': {'table_name': 'T000'},
            }}]},
            {'content': 'Tabella verificata dal sistema SAP.'},
        ], server_id='sap-on-premise', dynamic=True)
        call.assert_awaited_once_with(name='adt_get_table', arguments={'table_name': 'T000'})
        self.assertEqual(snapshot['server_id'], 'sap-on-premise')
        self.assertIn('unica fonte di verita', requests[0]['messages'][0]['content'])
        self.assertEqual(requests[0]['tools'][0]['function']['name'], 'adt_get_table')

    def test_repeated_calls_and_missing_tool_calls_are_rejected(self):
        call = {'content': '', 'tool_calls': [{'function': {'name': 'fetch', 'arguments': {'id': '/doc'}}}]}
        with self.assertRaisesRegex(ExplorerError, 'ciclo'):
            self.run_agent([call, call])
        with self.assertRaisesRegex(ExplorerError, 'non ha richiesto tool'):
            self.run_agent([{'content': 'Risposta senza fonti'}, {'content': 'Ancora senza tool'}])

    def test_json_text_tool_call_is_validated_and_executed(self):
        snapshot, call, _ = self.run_agent([
            {'content': '```json\n{"name":"fetch","arguments":{"id":"/doc"}}\n```'},
            {'content': 'Sintesi verificata dal documento.'},
        ])
        call.assert_awaited_once_with(name='fetch', arguments={'id': '/doc'})
        self.assertIn('Sintesi verificata', snapshot['raw_text'])
        self.assertEqual(llm_engine.text_tool_calls('Testo con {"name":"fetch","arguments":{}}'), [])
        with self.assertRaisesRegex(ExplorerError, 'non autorizzato'):
            self.run_agent([{'content': '{"name":"shell","arguments":{"command":"test"}}'}])

    def test_unlisted_model_is_rejected_before_mcp_or_inference(self):
        with patch('llm_engine.local_model_catalog', new=AsyncMock(return_value=[])):
            with patch('llm_engine.mcp_catalog', new=AsyncMock()) as catalog:
                with self.assertRaisesRegex(ExplorerError, 'non e installato'):
                    anyio.run(llm_engine.run_local_llm, 'unlisted-model', 'Richiesta SAP')
                catalog.assert_not_awaited()


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.LocalServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path, data=None, token=app.TOKEN, host=None):
        headers = {'Content-Type': 'application/json', 'X-App-Token': token}
        if host:
            headers['Host'] = host
        request = urllib.request.Request(
            self.url + path, headers=headers,
            data=None if data is None else json.dumps(data).encode(),
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_health_and_host_restriction(self):
        status, payload = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)['ready'])
        self.assertEqual(self.request('/api/health', host='evil.example')[0], 403)

    def test_server_and_tool_catalog_endpoints(self):
        servers = {'servers': [
            {'id': 'sap-cloud', 'label': 'SAP Cloud'},
            {'id': 'sap-on-premise', 'label': 'SAP On-Premise'},
        ], 'default_server': 'sap-cloud'}
        catalog = {'server_id': 'sap-on-premise', 'dynamic_arguments': True, 'tools': [{'name': 'adt_table'}]}
        with patch('app.list_servers', return_value=servers):
            status, payload = self.request('/api/servers')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), servers)
        with patch('app.list_server_tools', new=AsyncMock(return_value=catalog)) as mocked:
            status, payload = self.request('/api/tools?server_id=sap-on-premise')
            mocked.assert_awaited_once_with('sap-on-premise', app.PROVIDER_FACTORY)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), catalog)
        with patch('app.list_server_tools', new=AsyncMock(side_effect=ExplorerError('ADT offline'))):
            status, payload = self.request('/api/tools?server_id=sap-on-premise')
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(payload)['error'], 'ADT offline')
        options = {
            'SAP_URL': 'https://sap.example.local', 'SAP_CLIENT': '100',
            'SAP_SYSNR': '00', 'SAP_SYSTEM_ID': 'DS4',
        }
        with patch('app.PROVIDER_FACTORY', return_value=SimpleNamespace(env=options)) as factory:
            with patch('app.list_server_tools', new=AsyncMock(return_value=catalog)) as mocked:
                status, payload = self.request('/api/tools', {
                    'server_id': 'sap-on-premise', 'connection_options': options,
                })
                args = mocked.await_args.args
                simulated_provider = args[1]('sap-on-premise')
                factory.assert_called_once_with('sap-on-premise', options)
        self.assertEqual(status, 200)
        self.assertEqual(args[0], 'sap-on-premise')
        self.assertEqual(simulated_provider.env['SAP_URL'], options['SAP_URL'])
        self.assertEqual(simulated_provider.env['SAP_SYSNR'], options['SAP_SYSNR'])
        self.assertEqual(simulated_provider.env['SAP_SYSTEM_ID'], options['SAP_SYSTEM_ID'])

    def test_diagnostics_endpoint_returns_safe_report(self):
        report = {'summary': 'OK', 'authentication': {'status': 'OK'}, 'toolsFound': 4}
        options = {
            'SAP_URL': 'https://sap.example.local', 'SAP_USERNAME': 'BCECCARELLI',
            'SAP_PASSWORD': 'temporary-secret', 'SAP_CLIENT': '100',
            'SAP_SYSNR': '00', 'SAP_SYSTEM_ID': 'DS4',
        }
        with patch('app.diagnose_sap_onpremise', new=AsyncMock(return_value=report)) as mocked:
            status, payload = self.request('/api/diagnostics', {'connection_options': options})
            mocked.assert_awaited_once_with(options, app.PROVIDER_FACTORY)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), report)
        self.assertNotIn('temporary-secret', payload.decode())
    def test_token_and_invalid_inputs(self):
        for token in ('', 'invalid', '\u00e9'):
            self.assertEqual(self.request('/api/search', {'query': 'ABAP'}, token=token)[0], 403)
        self.assertEqual(self.request('/api/search', {'query': ''})[0], 400)
        self.assertEqual(self.request('/api/export', {'result_id': 'unknown', 'format': 'json'})[0], 400)

    def test_search_export_and_download(self):
        snapshot = {
            'query': 'SELECT', 'raw_text': 'SELECT statements in ABAP Cloud',
            'created_at': '2026-10-19T12:00:00+00:00', 'endpoint': app.ENDPOINT,
            'response': {'content': []},
            'optimized_query': 'SELECT', 'attempts': [{'query': 'SELECT', 'score': 0.08, 'result_count': 1}],
            'search_log': ['Query ottimizzata in: SELECT'],
        }
        with patch('app.invoke_tool', new=AsyncMock(return_value=snapshot)) as mocked:
            status, payload = self.request('/api/search', {'query': 'SELECT', 'count': 3})
            mocked.assert_awaited_once_with('search', 'SELECT', 3, 'public_cloud', None, app.PROVIDER_FACTORY)
        self.assertEqual(status, 200)
        result = json.loads(payload)
        self.assertEqual(result['search_log'], snapshot['search_log'])
        self.assertEqual(result['attempts'], snapshot['attempts'])
        self.assertEqual(result['query'], snapshot['query'])
        with tempfile.TemporaryDirectory() as temporary:
            with patch('app.BASE_DIR', Path(temporary)):
                status, payload = self.request('/api/export', {'result_id': result['result_id'], 'format': 'json'})
                self.assertEqual(status, 200)
                exported = json.loads(payload)
                self.assertTrue(Path(exported['path']).is_file())
                status, payload = self.request(exported['download_url'])
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload), snapshot)
                self.assertEqual(self.request('/api/download/../../app.py')[0], 404)

    def test_exclusive_bind(self):
        with self.assertRaises(OSError):
            duplicate = app.LocalServer(('127.0.0.1', self.server.server_port), app.Handler)
            duplicate.server_close()

    def test_invoke_endpoint_for_all_tools(self):
        for tool, text in (
            ('search', 'SELECT'), ('fetch', '/library/doc'),
            ('abap_lint', 'REPORT z_demo.\n' + ' ' * 40000),
            ('sap_community_search', 'SAP topic'), ('sap_search_objects', 'CL_ABAP_REGEX'),
            ('sap_get_object_details', 'CLAS, CL_ABAP_REGEX'),
            ('abap_feature_matrix', ''), ('sap_discovery_center_search', 'SAP Build'),
            ('sap_discovery_center_service', 'service-uuid'),
        ):
            snapshot = {
                'tool': tool, 'query': text, 'arguments': tool_arguments(tool, text),
                'raw_text': 'Risposta SAP', 'response': {'content': []},
                'created_at': '2026-10-19T12:00:00+00:00',
            }
            with self.subTest(tool=tool), patch('app.invoke_tool', new=AsyncMock(return_value=snapshot)) as mocked:
                status, payload = self.request('/api/invoke', {'tool': tool, 'text': text})
                self.assertEqual(status, 200)
                mocked.assert_awaited_once_with(tool, text, 3, 'public_cloud', None, app.PROVIDER_FACTORY)
                self.assertEqual(json.loads(payload)['tool'], tool)
                self.assertEqual(json.loads(payload)['arguments'], snapshot['arguments'])
        snapshot = {
            'tool': 'sap_get_object_details', 'query': 'FUNC, BAPI_EMPLOYEE_GETLIST',
            'arguments': tool_arguments('sap_get_object_details', 'FUNC, BAPI_EMPLOYEE_GETLIST', 'on_premise'),
            'raw_text': 'Risposta SAP', 'response': {}, 'created_at': '2026-10-19T12:00:00+00:00',
        }
        with patch('app.invoke_tool', new=AsyncMock(return_value=snapshot)) as mocked:
            status, payload = self.request('/api/invoke', {
                'tool': 'sap_get_object_details', 'text': 'FUNC, BAPI_EMPLOYEE_GETLIST',
                'system_type': 'on_premise', 'server_id': 'sap-on-premise',
            })
            self.assertEqual(status, 200)
            mocked.assert_awaited_once_with(
                'sap_get_object_details', 'FUNC, BAPI_EMPLOYEE_GETLIST', 3, 'on_premise',
                'sap-on-premise', app.PROVIDER_FACTORY,
            )
            self.assertEqual(json.loads(payload)['tool'], 'sap_get_object_details')
        self.assertEqual(self.request('/api/invoke', {'tool': 'unknown', 'text': 'SAP'})[0], 400)

    def test_llm_api_and_model_catalog(self):
        models = [{'name': 'local', 'size': 100, 'capabilities': ['completion', 'tools']}]
        with patch('app.list_local_models', new=AsyncMock(return_value=models)):
            status, payload = self.request('/api/models')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload)['models'], models)
        snapshot = {
            'mode': 'llm', 'tool': 'llm', 'model': 'local', 'query': 'Richiesta',
            'raw_text': 'Sintesi', 'mcp_raw_text': 'Dati MCP',
            'response': {'answer': 'Sintesi'}, 'created_at': '2026-10-19T12:00:00Z',
        }
        with patch('app.run_local_llm', new=AsyncMock(return_value=snapshot)) as mocked:
            status, payload = self.request('/api/invoke', {'mode': 'llm', 'model': 'local', 'text': 'Richiesta'})
            self.assertEqual(status, 200)
            mocked.assert_awaited_once_with('local', 'Richiesta', None, app.PROVIDER_FACTORY)
            self.assertEqual(json.loads(payload)['mcp_raw_text'], 'Dati MCP')
        self.assertEqual(self.request('/api/invoke', {'mode': 'unknown', 'text': 'Richiesta'})[0], 400)

    def test_ollama_unavailable_does_not_disable_python(self):
        with patch('app.list_local_models', new=AsyncMock(side_effect=ExplorerError('Ollama offline'))):
            self.assertEqual(self.request('/api/models')[0], 503)
        self.assertEqual(self.request('/api/health')[0], 200)


if __name__ == '__main__':
    unittest.main()