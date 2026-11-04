"""Consumer-only service policy; disposable conftest SQLite, no real POS writes."""
from datetime import datetime, time, timezone

import pytest
from sqlalchemy import select, delete
from app.database import SessionLocal
from app.models import Business, ServiceSchedule, ScheduleShift, Branch
from test_restaurant_agent_boundary import agent_headers, leased
from test_agent_schedule_boundary448 import body, counts


def replace_hours(tenant, variant):
    with SessionLocal.begin() as db:
        ids=list(db.scalars(select(ServiceSchedule.id).where(ServiceSchedule.branch_id==tenant['branch_id'])))
        db.execute(delete(ScheduleShift).where(ScheduleShift.schedule_id.in_(ids)))
        db.execute(delete(ServiceSchedule).where(ServiceSchedule.id.in_(ids)))
        if variant=='missing':return
        s=ServiceSchedule(business_id=tenant['business_id'],branch_id=tenant['branch_id'],kind='primary',
            name='Horario de prueba',active=variant!='inactive')
        db.add(s);db.flush()
        if variant!='empty':db.add(ScheduleShift(schedule_id=s.id,day_of_week=0,starts_at=time(22),ends_at=time(2)))
        if variant=='timezone':db.get(Business,tenant['business_id']).timezone='Invalid/Restaurant'


@pytest.mark.parametrize('variant',['missing','inactive','empty','timezone'])
@pytest.mark.parametrize('channel',['takeaway','delivery'])
@pytest.mark.parametrize('method',['cash','yape'])
def test_unverifiable_context_never_authorizes_purchase(client,tenant,agent_headers,monkeypatch,variant,channel,method):
    from app import settings_service
    monkeypatch.setattr(settings_service,'utcnow',lambda:datetime(2026,10,20,4,tzinfo=timezone.utc))
    replace_hours(tenant,variant)
    context=client.get('/api/v1/integrations/context',headers=agent_headers)
    assert context.status_code==200,context.text
    data=context.json();service=data['service_availability']
    assert data['open_now'] is None and service['status']=='unverified'
    assert service['reason']=={'missing':'missing_schedule','inactive':'inactive_schedule','empty':'empty_schedule','timezone':'invalid_timezone'}[variant]
    assert service['business_id']==tenant['business_id'] and service['branch_id']==tenant['branch_id'] and service['checked_at']
    catalog=client.get('/api/v1/integrations/context/catalog',headers=agent_headers)
    assert catalog.status_code==200,catalog.text
    assert catalog.json()['open_now'] is None
    assert catalog.json()['service_availability']['status']=='unverified'
    assert all(not p['available'] for p in catalog.json()['products'])
    _,headers=leased(client,agent_headers)
    preview=client.post('/api/v1/integrations/orders/preview',headers=headers,json={
        'channel':channel,'payment_method':method,'items':body(tenant)['items']})
    payload=body(tenant,method);payload['channel']=channel
    if channel=='delivery':payload['delivery_address']={'address':'Calle sintética 123','reference':'Laboratorio'}
    draft=client.post('/api/v1/integrations/orders/draft',headers={**headers,'Idempotency-Key':'unverified'},json=payload)
    for r in [preview,draft]:assert r.status_code==409 and r.json()['code']=='SERVICE_UNVERIFIED',r.text
    assert counts()=={'orders':0,'kitchen_tickets':0,'payments':0}


@pytest.mark.parametrize('at,expected',[(datetime(2026,10,20,4,tzinfo=timezone.utc),'open'),
    (datetime(2026,10,20,6,59,tzinfo=timezone.utc),'open'),(datetime(2026,10,20,7,tzinfo=timezone.utc),'closed')])
def test_overnight_uses_restaurant_timezone_and_exact_closing(client,tenant,agent_headers,monkeypatch,at,expected):
    from app import settings_service
    monkeypatch.setattr(settings_service,'utcnow',lambda:at)
    replace_hours(tenant,'overnight')
    data=client.get('/api/v1/integrations/context',headers=agent_headers).json()
    assert data['service_availability']['status']==expected
    assert data['open_now'] is (expected=='open')
    assert data['service_availability']['timezone']=='America/Lima'
    assert data['service_availability']['schedule_versions'][0]['active'] is True


@pytest.mark.parametrize('day,name,next_name',[(0,'lunes','martes'),(1,'martes','miércoles'),
    (2,'miércoles','jueves'),(3,'jueves','viernes'),(4,'viernes','sábado'),
    (5,'sábado','domingo'),(6,'domingo','lunes')])
def test_agent_hours_publish_unambiguous_weekdays_and_overnight_intervals(
    client,tenant,agent_headers,monkeypatch,day,name,next_name,
):
    from app import settings_service
    monkeypatch.setattr(settings_service,'utcnow',lambda:datetime(2026,10,23,23,44,tzinfo=timezone.utc))
    replace_hours(tenant,'overnight')
    with SessionLocal.begin() as db:
        db.get(Business,tenant['business_id']).timezone='Europe/London'
        shift=db.scalar(select(ScheduleShift));shift.day_of_week=day
    data=client.get('/api/v1/integrations/context',headers=agent_headers).json()
    schedule=data['schedules'][0];shift=schedule['shifts'][0]
    assert schedule['weekday_numbering']=='monday_zero'
    assert shift['day_name']==name and shift['ends_day_name']==next_name
    assert shift['spans_next_day'] is True and shift['duration_minutes']==240
    assert schedule['display_intervals']==[f'{name} de 22:00 a 02:00 del {next_name} siguiente']
    if day==4:assert data['service_availability']['status']=='open'


def test_agent_hours_distinguish_a_full_day_from_an_unknown_interval(client,tenant,agent_headers):
    replace_hours(tenant,'overnight')
    with SessionLocal.begin() as db:
        shift=db.scalar(select(ScheduleShift));shift.starts_at=time(0);shift.ends_at=time(0)
    data=client.get('/api/v1/integrations/context',headers=agent_headers).json()
    shift=data['schedules'][0]['shifts'][0]
    assert shift['duration_minutes']==1440 and shift['spans_next_day'] is True
    assert shift['display']=='lunes de 00:00 a 00:00 del martes siguiente (24 horas)'


def test_generic_schedule_contract_keeps_its_original_projection(client,tenant,auth_headers):
    from test_escalar_integrations import create_credential,integration_headers
    replace_hours(tenant,'overnight')
    credential=create_credential(client,tenant,auth_headers,['menu:read'])
    data=client.get('/api/v1/integrations/context',headers=integration_headers(credential['token'])).json()
    assert 'weekday_numbering' not in data['schedules'][0]
    assert 'display_intervals' not in data['schedules'][0]
    assert 'day_name' not in data['schedules'][0]['shifts'][0]


@pytest.mark.parametrize('number,holder,missing',[(None,'Titular',['number']),('000000000',None,['recipient_name']),
    (None,None,['number','recipient_name']),('000000000','Titular',[])])
def test_yape_completeness_is_read_from_current_branch(client,tenant,agent_headers,number,holder,missing):
    with SessionLocal.begin() as db:
        branch=db.get(Branch,tenant['branch_id']);branch.yape_number=number;branch.payment_recipient_name=holder
    yape=client.get('/api/v1/integrations/context',headers=agent_headers).json()['payments']['yape']
    assert yape['missing_fields']==missing and yape['instructions_ready'] is (not missing)
    revision=yape['revision']
    with SessionLocal.begin() as db:db.get(Branch,tenant['branch_id']).payment_recipient_name='Titular actualizado'
    latest=client.get('/api/v1/integrations/context',headers=agent_headers).json()['payments']['yape']
    assert latest['recipient_name']=='Titular actualizado' and latest['revision']!=revision


@pytest.mark.parametrize('variant',['missing','inactive','empty','timezone'])
def test_existing_order_survives_unknown_hours_but_new_addition_is_rejected(client,tenant,agent_headers,variant):
    _,headers=leased(client,agent_headers)
    created=client.post('/api/v1/integrations/orders/draft',headers={**headers,'Idempotency-Key':'open-base'},json=body(tenant))
    assert created.status_code==201,created.text
    order=created.json()
    confirmed=client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",headers={**headers,'Idempotency-Key':'confirm-base'})
    assert confirmed.status_code==200,confirmed.text
    before=counts()
    replace_hours(tenant,variant)
    current=client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",headers=headers,params={'sender':'51999900001'})
    assert current.status_code==200,current.text
    addition=client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches",headers={**headers,'Idempotency-Key':'unknown-addition'},json={
        'sender':'51999900001','expected_version':current.json()['version'],'payment_method':'cash','expected_amount':20,'items':body(tenant)['items']})
    assert addition.status_code==409 and addition.json()['code']=='SERVICE_UNVERIFIED',addition.text
    assert counts()==before


def test_schedule_configuration_change_is_visible_without_reusing_previous_status(client,tenant,agent_headers,monkeypatch):
    from app import settings_service
    monkeypatch.setattr(settings_service,'utcnow',lambda:datetime(2026,10,20,4,tzinfo=timezone.utc))
    replace_hours(tenant,'overnight')
    first=client.get('/api/v1/integrations/context',headers=agent_headers).json()
    assert first['service_availability']['status']=='open'
    with SessionLocal.begin() as db:
        schedule=db.scalar(select(ServiceSchedule).where(ServiceSchedule.branch_id==tenant['branch_id']))
        schedule.active=False;schedule.version+=1
    second=client.get('/api/v1/integrations/context',headers=agent_headers).json()
    assert second['service_availability']['status']=='unverified'
    assert second['service_availability']['schedule_versions'][0]['version']>first['service_availability']['schedule_versions'][0]['version']


def test_catalog_version_tracks_configuration_not_the_clock_tick(client,tenant,agent_headers,monkeypatch):
    from app import settings_service
    now={'at':datetime(2026,10,20,4,tzinfo=timezone.utc)}
    monkeypatch.setattr(settings_service,'utcnow',lambda:now['at'])
    replace_hours(tenant,'overnight')
    first=client.get('/api/v1/integrations/context/catalog',headers=agent_headers).json()
    now['at']=datetime(2026,10,20,4,1,tzinfo=timezone.utc)
    second=client.get('/api/v1/integrations/context/catalog',headers=agent_headers).json()
    assert first['service_availability']['checked_at']!=second['service_availability']['checked_at']
    assert first['catalog_version']==second['catalog_version']


def test_yape_revision_change_during_image_download_never_returns_old_qr(client,tenant,agent_headers,monkeypatch):
    from app import api
    with SessionLocal.begin() as db:
        branch=db.get(Branch,tenant['branch_id'])
        branch.yape_number='000000001';branch.payment_recipient_name='Titular inicial'
        branch.yape_qr_storage_path='synthetic/private/original.png'
    revision=client.get('/api/v1/integrations/context',headers=agent_headers).json()['payments']['yape']['revision']
    async def changed_while_downloading(path):
        assert path=='synthetic/private/original.png'
        with SessionLocal.begin() as db:
            branch=db.get(Branch,tenant['branch_id'])
            branch.yape_number='000000002';branch.payment_recipient_name='Titular actualizado'
            branch.yape_qr_storage_path='synthetic/private/updated.png'
        return b'old image must not escape','image/png'
    monkeypatch.setattr(api,'load_private_file',changed_while_downloading)
    image=client.get('/api/v1/integrations/context/yape-qr',headers=agent_headers,params={'expected_revision':revision})
    assert image.status_code==409 and image.json()['code']=='YAPE_CONFIGURATION_STALE',image.text
    assert b'old image must not escape' not in image.content
    latest=client.get('/api/v1/integrations/context',headers=agent_headers).json()['payments']['yape']
    assert latest['number']=='000000002' and latest['recipient_name']=='Titular actualizado' and latest['revision']!=revision


@pytest.mark.parametrize('zone,expected',[('America/Lima','closed'),('Asia/Tokyo','open')])
def test_identical_server_instant_uses_each_restaurant_timezone(client,tenant,agent_headers,monkeypatch,zone,expected):
    from app import settings_service
    monkeypatch.setattr(settings_service,'utcnow',lambda:datetime(2026,10,19,16,tzinfo=timezone.utc))
    replace_hours(tenant,'overnight')
    with SessionLocal.begin() as db:db.get(Business,tenant['business_id']).timezone=zone
    data=client.get('/api/v1/integrations/context',headers=agent_headers).json()
    assert data['service_availability']['status']==expected
    assert data['service_availability']['timezone']==zone
