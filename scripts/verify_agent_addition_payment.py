"""New ASGI boundary cases only. SQLite memory, no auth/provider/printer traffic."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import unittest
from datetime import time
from decimal import Decimal

args = argparse.ArgumentParser()
args.add_argument('--source', required=True)
args.add_argument('--output', required=True)
opts = args.parse_args()
source = Path(opts.source).resolve()
output = Path(opts.output).resolve()
output.mkdir(exist_ok=False)
sys.dont_write_bytecode = True
sys.path.insert(0, str(source))
network_attempts, calls = [], []
original_connect, socketpair_code = socket.socket.connect, socket.socketpair.__code__


def forbidden(*a, **kw):
    network_attempts.append('forbidden')
    raise AssertionError('C472_EXTERNAL_NETWORK_FORBIDDEN')


def connect(sock, address):
    caller = sys._getframe(1)
    listener = caller.f_locals.get('lsock')
    if (caller.f_code is socketpair_code and caller.f_locals.get('csock') is sock
            and isinstance(listener, socket.socket) and address[0] in {'127.0.0.1', '::1'}
            and tuple(address[:2]) == tuple(listener.getsockname()[:2])):
        return original_connect(sock, address)
    return forbidden(sock, address)


socket.socket.connect = connect
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
system_root = os.environ.get('SystemRoot', r'C:\Windows')
os.environ.clear()
os.environ.update(SystemRoot=system_root, ESCALAR_ENV_FILE_DISABLED='true', ENVIRONMENT='test',
                  DATABASE_URL='sqlite+pysqlite:///:memory:', AUTO_CREATE_SCHEMA='false',
                  UPLOAD_DIR=str(output / 'unused-storage'))
os.chdir(output)
assert sys.version_info[:2] == (3, 12)

from fastapi.testclient import TestClient
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.main import app
from app.database import Base, get_db
from app.auth import IntegrationAuthContext
from app import agent_checkout_api as checkout
from app.agent_checkout import fingerprint, serialize_request
from app.services import save_idempotent_response
from app.models import (Business, Branch, BranchSettings, Product, Order, OrderItem,
    OrderPaymentRequest, KitchenTicket, PrintJob, ServiceSchedule, ScheduleShift,
    CashRegister, CashSession)

PHONE = '51900000472'
route = next(r for r in checkout.router.routes if getattr(r, 'path', None) == '/api/v1/integrations/orders/{order_id}/item-batches')
integration_dependency = next(d.call for d in route.dependant.dependencies if d.name == 'integration')


class BoundaryCases(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:',
            connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.profile = 'restaurant_agent'
        self.broadcasts = []
        self.original_broadcast = checkout.hub.broadcast
        async def broadcast(branch, name, payload):
            self.broadcasts.append(name)
        checkout.hub.broadcast = broadcast
        def db_dependency():
            with self.sessions() as db:
                yield db
        def integration():
            return IntegrationAuthContext(1, 47, 83, frozenset({'orders:write'}),
                profile=self.profile, sender=PHONE)
        app.dependency_overrides[get_db] = db_dependency
        app.dependency_overrides[integration_dependency] = integration
        self.client = TestClient(app)
        with self.sessions() as db:
            db.add(Business(id=47, slug='c472-own', name='Synthetic', timezone='UTC'))
            db.flush()
            db.add(Branch(id=83, business_id=47, slug='c472-own', name='Synthetic'))
            db.flush()
            db.add(BranchSettings(business_id=47, branch_id=83, digital_takeaway=True,
                payment_methods={k: ['cash', 'yape'] for k in ['counter', 'takeaway', 'delivery']},
                printer_config={'automatic_printing': False}))
            db.add_all([Product(id=91, business_id=47, branch_id=83, sku='BASE', name='Base', price=20),
                Product(id=92, business_id=47, branch_id=83, sku='EXTRA', name='Addition', price=7)])
            schedule = ServiceSchedule(business_id=47, branch_id=83, name='Synthetic', kind='primary', active=True)
            db.add(schedule)
            db.flush()
            db.add_all([ScheduleShift(schedule_id=schedule.id, day_of_week=day,
                starts_at=time(0), ends_at=time(23, 59, 59, 999999)) for day in range(7)])
            db.add(CashRegister(id=101, business_id=47, branch_id=83, name='Synthetic'))
            db.flush()
            db.add(CashSession(business_id=47, branch_id=83, register_id=101,
                status='open', opening_amount=0, opened_by='c472'))
            db.commit()

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        checkout.hub.broadcast = self.original_broadcast
        self.engine.dispose()
        self.assertEqual(network_attempts, [])

    def order(self, method):
        with self.sessions() as db:
            order = Order(business_id=47, branch_id=83, number='C472-' + method,
                source='whatsapp_agent', channel='takeaway', status='confirmed',
                payment_method=method, payment_status='pending', customer_phone=PHONE,
                whatsapp_chat_id=PHONE + '@c.us', subtotal=20, total=20, delivery_fee=0)
            order.items = [OrderItem(product_id=91, product_name='Base', quantity=1,
                                    unit_price=20, line_total=20)]
            db.add(order)
            db.commit()
            return order.id, order.version

    def snapshot(self):
        with self.engine.connect() as db:
            values = {table.name: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                for row in db.execute(select(table)).mappings()) for table in Base.metadata.sorted_tables}
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def post(self, order_id, version, *, method='OMIT', key='c472-new'):
        body = {'sender': PHONE, 'expected_version': version, 'expected_amount': 7,
                'items': [{'product_id': 92, 'quantity': 1}]}
        if method != 'OMIT':
            body['payment_method'] = method
        response = self.client.post(f'/api/v1/integrations/orders/{order_id}/item-batches',
            json=body, headers={'Idempotency-Key': key})
        calls.append({'base_id': order_id, 'method_input': method,
            'status': response.status_code, 'body': response.json()})
        return response

    def test_01_new_agent_addition_without_choice_has_no_business_effect(self):
        # Before the guard the original Yape method silently creates an addition request.
        for original in ['yape', 'cash']:
            order_id, version = self.order(original)
            for supplied in ['OMIT', None]:
                with self.subTest(original=original, supplied=supplied):
                    before = self.snapshot()
                    response = self.post(order_id, version, method=supplied,
                        key='c472-no-choice-' + original + '-' + str(supplied))
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(response.json().get('code'), 'ADDITION_PAYMENT_METHOD_REQUIRED')
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.broadcasts, [])

    def test_02_explicit_choice_can_differ_from_original_and_replay_is_stable(self):
        for original, chosen in [('cash', 'yape'), ('yape', 'cash')]:
            with self.subTest(original=original, chosen=chosen):
                order_id, version = self.order(original)
                key = 'c472-explicit-' + chosen
                response = self.post(order_id, version, method=chosen, key=key)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['payment_method'], chosen)
                self.assertEqual(response.json()['payment_request']['method'], chosen)
                self.assertFalse(response.json()['sent_to_kitchen'])
                before, events = self.snapshot(), len(self.broadcasts)
                duplicate = self.post(order_id, version, method=chosen, key=key)
                self.assertEqual(duplicate.status_code, 200, duplicate.text)
                self.assertEqual(duplicate.json(), response.json())
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(self.broadcasts), events)

    def test_03_historical_omitted_method_replay_returns_saved_result_only(self):
        order_id, version = self.order('yape')
        payload = checkout.Addition(sender=PHONE, expected_version=version,
            expected_amount=Decimal('7'), items=[{'product_id': 92, 'quantity': 1}])
        content = payload.model_dump(mode='json')
        content.pop('payment_method')
        key = 'c472-historical-omitted'
        with self.sessions() as db:
            order = db.get(Order, order_id)
            request = OrderPaymentRequest(business_id=47, branch_id=83, order_id=order_id,
                purpose='addition', method='yape', amount=7, status='pending', snapshot={})
            db.add(request)
            db.flush()
            saved = {'payment_method': 'yape', 'payment_request': serialize_request(request),
                'sent_to_kitchen': False, 'command_committed': False, 'tickets': [],
                'appended_item_ids': [], 'addition_amount': 7}
            order.version += 1
            save_idempotent_response(db, f'agent-addition:{order_id}', key, 47,
                {'request_digest': fingerprint(content), 'result': saved})
            db.commit()
        saved = jsonable_encoder(saved)
        before = self.snapshot()
        response = self.post(order_id, version, key=key)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), saved)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.broadcasts, [])


result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BoundaryCases))
summary = {'status': 'PASS' if result.wasSuccessful() else 'FAIL_PRESERVED',
    'python': sys.version, 'source': str(source), 'tests': result.testsRun,
    'failures': len(result.failures), 'errors': len(result.errors), 'calls': calls,
    'failure_details': [{'test': str(t), 'traceback': trace} for t, trace in result.failures + result.errors],
    'network_attempts': len(network_attempts), 'auth_transport_mocked': True,
    'SQLite_memory': True, 'model_GLOBAL_real_POS_payments_paper': 0,
    'whole_agent_accepted': False}
(output / 'result.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
print(json.dumps({k: summary[k] for k in ['status', 'tests', 'failures', 'errors', 'network_attempts']}))
raise SystemExit(0 if result.wasSuccessful() else 1)
