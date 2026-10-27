"""Deliver an authorized 1Password field through the existing guest keyboard."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Sequence
from uuid import UUID

from . import keys
from .backend import LibvirtBackend
from .vm import Controller

_MESSAGES = {
    'invalid_arguments': 'Check the command and required options.',
    'invalid_config': 'Provide a valid credential configuration with Agents, Shared Vault and VM UUIDs.',
    'denied_vm': 'Choose a VM alias from the credential configuration.',
    'inaccessible_item': 'Choose a current item UUID from the configured vaults.',
    'op_unavailable': 'Install 1Password CLI and make op executable on PATH.',
    'op_timeout': '1Password CLI timed out. Check authentication and connectivity.',
    'authentication_failed': 'Unlock or authenticate 1Password and check configured vault access.',
    'service_account_unavailable': 'Configure the local service account credential in the keyring.',
    'unavailable_field': 'Choose an available field ID or an exact unique field label.',
    'invalid_input': 'The selected value must contain only printable ANSI US keyboard characters.',
    'backend_failure': 'Check local libvirt access and the configured VM UUID.',
    'input_may_be_partial': 'Guest input may be partial. Check the focused field before any manual retry.',
    'interrupted': 'The request was interrupted before guest input.',
    'op_failed': '1Password returned an invalid response. Check CLI access and try again.',
}


class CredentialError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CredentialError('invalid_arguments')

    def print_help(self, file: Any = None) -> None:
        print(json.dumps({'usage': 'qemu-credential {list,type} [--config PATH] [--vm NAME] [--item ID] [--field password]'}))


@dataclass(frozen=True)
class Configuration:
    account: str
    vaults: dict[str, str]
    libvirt_uri: str
    vms: dict[str, str]
    service_account_keyring: bool = False


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[a-z0-9]{26}', value) is not None


def _configuration(path: str) -> Configuration:
    try:
        data = json.loads(Path(path).expanduser().read_text())
        if not isinstance(data, dict):
            raise ValueError
        account = data.get('account')
        if not isinstance(account, str) or re.fullmatch(r'[a-zA-Z0-9]{26}', account) is None:
            raise ValueError
        entries = data['vaults']
        if not isinstance(entries, list) or len(entries) != 2:
            raise ValueError
        vaults: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not _identifier(entry.get('id')) or entry.get('name') not in ('Agents', 'Shared Vault'):
                raise ValueError
            vaults[entry['id']] = entry['name']
        if len(vaults) != 2 or set(vaults.values()) != {'Agents', 'Shared Vault'}:
            raise ValueError
        service_account_keyring = data.get('service_account_keyring', False)
        if not isinstance(service_account_keyring, bool):
            raise ValueError
        uri = data.get('libvirt_uri', 'qemu:///system')
        if uri not in ('qemu:///system', 'qemu:///session'):
            raise ValueError
        aliases = data['vms']
        if not isinstance(aliases, dict):
            raise ValueError
        vms: dict[str, str] = {}
        for alias, identifier in aliases.items():
            if not isinstance(alias, str) or not alias or not isinstance(identifier, str):
                raise ValueError
            vms[alias] = str(UUID(identifier))
        return Configuration(account, vaults, uri, vms, service_account_keyring)
    except (OSError, ValueError, KeyError, TypeError):
        raise CredentialError('invalid_config') from None


def _op(configuration: Configuration, arguments: list[str], *, failure: str = 'authentication_failed') -> str:
    # Connect mode changes the source of data. Keep desktop sessions and service
    # account authentication, but never inherit a vault or account policy.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('OP_CONNECT_') and key not in
                   {'OP_VAULT', 'OP_INCLUDE_ARCHIVE', 'OP_ACCOUNT', 'OP_DEBUG', 'OP_LOG_LEVEL'}}
    if configuration.service_account_keyring:
        try:
            gi = importlib.import_module('gi')
            gi.require_version('Secret', '1')
            secret = importlib.import_module('gi.repository.Secret')
            attributes = {'application': 'qemu-credential', 'credential': 'service-account'}
            schema_attributes = {name: secret.SchemaAttributeType.STRING for name in attributes}
            schema = secret.Schema.new('local.qemu-credential', secret.SchemaFlags.NONE, schema_attributes)
            token = secret.password_lookup_sync(schema, attributes, None)
            if not isinstance(token, str) or not token.startswith('ops_'):
                raise ValueError
            environment['OP_SERVICE_ACCOUNT_TOKEN'] = token
        except Exception:
            raise CredentialError('service_account_unavailable') from None
    try:
        result = subprocess.run(['op', *arguments, '--account', configuration.account],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=30, env=environment)
    except subprocess.TimeoutExpired:
        raise CredentialError('op_timeout') from None
    except OSError:
        raise CredentialError('op_unavailable') from None
    if result.returncode:
        raise CredentialError(failure)
    return result.stdout


def _json_response(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        raise CredentialError('op_failed') from None


def _items(configuration: Configuration) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for vault_id, vault_name in configuration.vaults.items():
        result = _json_response(_op(configuration, ['item', 'list', '--vault', vault_id, '--format=json']))
        if not isinstance(result, list):
            raise CredentialError('op_failed')
        for item in result:
            if not isinstance(item, dict):
                raise CredentialError('op_failed')
            vault = item.get('vault')
            if not isinstance(vault, dict) or vault.get('id') != vault_id:
                continue
            if item.get('archived') or str(item.get('state', '')).upper() == 'ARCHIVED':
                continue
            if not _identifier(item.get('id')):
                raise CredentialError('op_failed')
            title, category = item.get('title', ''), item.get('category', '')
            if not isinstance(title, str) or not isinstance(category, str):
                raise CredentialError('op_failed')
            items.append({'id': item['id'], 'title': title,
                          'vault': {'id': vault_id, 'name': vault_name}, 'category': category})
    return items


def _item(configuration: Configuration, identifier: str, items: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    matches = [item for item in items if item['id'] == identifier]
    if len(matches) != 1:
        raise CredentialError('inaccessible_item')
    metadata = matches[0]
    details = _json_response(_op(configuration, ['item', 'get', identifier, '--vault', metadata['vault']['id'], '--format=json'],
                                 failure='inaccessible_item'))
    if (not isinstance(details, dict) or details.get('id') != identifier
            or not isinstance(details.get('vault'), dict)
            or details['vault'].get('id') != metadata['vault']['id']
            or details.get('archived') or str(details.get('state', '')).upper() == 'ARCHIVED'):
        raise CredentialError('inaccessible_item')
    return metadata, details


def _fields(details: dict[str, Any]) -> list[dict[str, Any]]:
    fields = details.get('fields', [])
    if (not isinstance(fields, list) or any(not isinstance(field, dict)
            or any(not isinstance(field.get(key), str) for key in ('id', 'label', 'type')) for field in fields)):
        raise CredentialError('op_failed')
    return fields


def _value(configuration: Configuration, metadata: dict[str, Any], details: dict[str, Any], selector: str) -> str:
    fields = _fields(details)
    matches = [field for field in fields if field['id'] == selector]
    if not matches:
        matches = [field for field in fields if field['label'] == selector]
    if not matches and selector == 'otp':
        matches = [field for field in fields if field['type'] == 'OTP']
    if len(matches) != 1:
        raise CredentialError('unavailable_field')
    value: object
    if matches[0]['type'] == 'OTP':
        # op --otp returns the primary code, so it cannot select among OTP fields.
        if sum(field['type'] == 'OTP' for field in fields) != 1:
            raise CredentialError('unavailable_field')
        # --otp emits one CLI framing newline; never trim stored field values.
        value = _op(configuration, ['item', 'get', metadata['id'], '--vault', metadata['vault']['id'], '--otp'],
                    failure='unavailable_field').removesuffix('\n')
    else:
        value = matches[0].get('value')
    if not isinstance(value, str) or not value:
        raise CredentialError('unavailable_field')
    try:
        for character in value:
            if ord(character) < 32 or ord(character) == 127:
                raise ValueError
            keys.char_to_keys(character)
    except ValueError:
        raise CredentialError('invalid_input') from None
    return value


def _type(configuration: Configuration, alias: str, value: str) -> dict[str, bool]:
    controller: Controller | None = None
    typing_started = False
    try:
        binding = importlib.import_module('libvirt')
        # libvirt otherwise prints native error details before raising Python errors.
        # Register only in this CLI path, before even attempting a connection.
        binding.registerErrorHandler(lambda _context, _error: None, None)
        controller = Controller(LibvirtBackend(configuration.libvirt_uri, binding=binding))
        identifier = configuration.vms[alias]
        attachment = controller.attach(identifier)
        if attachment['uuid'] != identifier:
            raise CredentialError('backend_failure')
        typing_started = True
        controller.type_text(identifier, value)
        return {'typed': True, 'guest_text_verified': False}
    except (Exception, KeyboardInterrupt):
        raise CredentialError('input_may_be_partial' if typing_started else 'backend_failure') from None
    finally:
        if controller is not None:
            try:
                controller.close()
            except (Exception, KeyboardInterrupt):
                pass


def _parser() -> argparse.ArgumentParser:
    parser = JsonParser(prog='qemu-credential', allow_abbrev=False)
    # A suppressed subparser default permits --config before or after the verb.
    parser.add_argument('--config', default=str(Path('~/.config/qemu-credential/config.json')))
    commands = parser.add_subparsers(dest='command', required=True, parser_class=JsonParser)
    for name in ('list', 'type'):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument('--config', default=argparse.SUPPRESS)
        command.add_argument('--vm', required=name == 'type')
        command.add_argument('--item', required=name == 'type')
        if name == 'type':
            command.add_argument('--field', default='password')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        configuration = _configuration(arguments.config)
        if arguments.vm is not None and arguments.vm not in configuration.vms:
            raise CredentialError('denied_vm')
        if arguments.item is not None and not _identifier(arguments.item):
            raise CredentialError('inaccessible_item')
        items = _items(configuration)
        result: dict[str, Any]
        if arguments.item is None:
            result = {'items': items}
        else:
            metadata, details = _item(configuration, arguments.item, items)
            if arguments.command == 'list':
                result = {'item': metadata, 'fields': [
                    {key: field[key] for key in ('id', 'label', 'type')} for field in _fields(details)]}
            else:
                value = _value(configuration, metadata, details, arguments.field)
                result = _type(configuration, arguments.vm, value)
        print(json.dumps(result))
        return 0
    except CredentialError as error:
        code = error.code
    except KeyboardInterrupt:
        code = 'interrupted'
    except Exception:
        code = 'op_failed'
    print(json.dumps({'error': {'code': code, 'message': _MESSAGES[code]}}))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
