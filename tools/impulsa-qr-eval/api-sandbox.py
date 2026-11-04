"""Explicit-start loopback adapter to the real API 1.1 TestClient.

No service starts on import. SQLite is memory-only. This adapter cannot select a
production DB, an external bind address, Supabase, a printer or WhatsApp.
"""
from __future__ import annotations
import argparse
from datetime import time
from decimal import Decimal
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import parse_qs, urlsplit

HERE=Path(__file__).resolve().parent
WORKSPACE=HERE.parents[2]

def create_sandbox():
    # Set these before importing app.database/config; no user .env is read.
    os.environ.update(ESCALAR_ENV_FILE_DISABLED="true",ENVIRONMENT="test",
        DATABASE_URL="sqlite+pysqlite:///:memory:",AUTO_CREATE_SCHEMA="false",
        DEV_AUTH_TOKEN="",INTEGRATION_SERVICE_TOKEN="",AUTH_ADMIN_SECRET="",
        LEGACY_PUBLIC_READS_ENABLED="false",SUPABASE_URL="",SUPABASE_JWT_SECRET="",
        SUPABASE_JWKS_URL="",SUPABASE_SERVICE_ROLE_KEY="",QZ_TRAY_CERTIFICATE="",
        QZ_TRAY_PRIVATE_KEY="",QZ_TRAY_CERTIFICATE_FILE="",QZ_TRAY_PRIVATE_KEY_FILE="")
    api_root=Path(os.environ.get("ESCALAR_QR_SANDBOX_API_ROOT",str(WORKSPACE/".worktrees/api-tools"))).resolve()
    assert api_root.is_absolute() and (api_root/"app/main.py").is_file()
    sys.path.insert(0,str(api_root))
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.auth import hash_integration_token
    from app.database import Base,SessionLocal,engine
    from app.main import app
    from app.models import (Business,Branch,BranchSettings,CashRegister,Category,Product,
        ProductVariant,ModifierGroup,Modifier,ProductModifierGroup,ServiceSchedule,ScheduleShift,
        IntegrationCredential,Order,Payment,KitchenTicket,PrintJob,AgentConversation,AgentMessageDelivery,
        AgentAttentionRequest,OrderPaymentRequest,PaymentEvidence,DeliveryAssignment,AgentModelUsage)
    from app.schemas import RESTAURANT_AGENT_SCOPES
    assert str(engine.url)=="sqlite+pysqlite:///:memory:"
    # Integration authentication selects the credential by the part before '.';
    # the per-run secret remains random and is never printed.
    tokens={identifier:f"smoke-{identifier}."+secrets.token_urlsafe(40) for identifier in (2,3)}
    control=secrets.token_urlsafe(40)
    control_state={"case_id":None,"reset_count":0}

    def seed(case_id):
        # Destruction is limited to this process's private in-memory engine.
        assert str(engine.url)=="sqlite+pysqlite:///:memory:"
        Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
        with SessionLocal.begin() as db:
            for identifier in (2,3):
                db.add(Business(id=identifier,slug=f"smoke-{identifier}",name="Pizza House SINTÉTICO" if identifier==2 else "Alitas SINTÉTICAS"));db.flush()
                db.add(Branch(id=identifier,business_id=identifier,slug="principal",name="Principal sintética",
                    address="Calle Ensayo 123, Lima",accepted_payment_methods=["cash","yape"],
                    yape_number="999000004",payment_recipient_name="TITULAR SINTÉTICO NO PAGAR"));db.flush()
                db.add(BranchSettings(business_id=identifier,branch_id=identifier,delivery_mode="quote",
                    payment_methods={mode:["cash","yape"] for mode in ("counter","takeaway","delivery")},
                    advanced_printing=True,printer_config={"printer_name":"SYNTHETIC-NO-PHYSICAL-PRINTER",
                        "paper_width_mm":80,"auto_print_kitchen":True,"manual_customer_receipt":False}))
                db.add(CashRegister(business_id=identifier,branch_id=identifier,name="Caja sintética",is_default=True))
                schedule=ServiceSchedule(business_id=identifier,branch_id=identifier,name="Horario sintético",kind="primary",active=True)
                db.add(schedule);db.flush()
                db.add_all([ScheduleShift(schedule_id=schedule.id,day_of_week=day,starts_at=time(0),ends_at=time(0)) for day in range(7)])
                category=Category(business_id=identifier,branch_id=identifier,name="Carta sintética");db.add(category);db.flush()
                configurable=Product(id=101 if identifier==2 else 201,business_id=identifier,branch_id=identifier,
                    category_id=category.id,sku=f"SYN-CONFIG-{identifier}",name="Pizza combinada" if identifier==2 else "Alitas",
                    price=20 if identifier==2 else 18,service_channels=["pos","digital_delivery","digital_takeaway"])
                db.add(configurable);db.flush()
                group=ModifierGroup(business_id=identifier,branch_id=identifier,name="Sabores" if identifier==2 else "Salsa",
                    required=True,minimum=2 if identifier==2 else 0,maximum=3 if identifier==2 else 1,allow_repeats=False)
                db.add(group);db.flush();db.add(ProductModifierGroup(product_id=configurable.id,group_id=group.id))
                if identifier==2:
                    db.add_all([ProductVariant(product_id=101,name="Mediana",price_delta=0),ProductVariant(product_id=101,name="Familiar",price_delta=8)])
                for index,name in enumerate(["Pepperoni","Hawaiana","Vegetariana","Americana"] if identifier==2 else ["BBQ","Búfalo"]):
                    db.add(Modifier(group_id=group.id,name=name,price_delta=0,available=not(identifier==3 and index==1)))
                extras=ModifierGroup(business_id=identifier,branch_id=identifier,name="Extras",minimum=0,maximum=2,required=False)
                db.add(extras);db.flush();db.add(ProductModifierGroup(product_id=configurable.id,group_id=extras.id))
                db.add(Modifier(group_id=extras.id,name="Queso extra",price_delta=3))
                for product_id,name,price,available in [(102 if identifier==2 else 202,"Agua sin gas",3,True),
                    (103 if identifier==2 else 203,"Gaseosa",5,True),(104 if identifier==2 else 204,"Jugo agotado",4,False)]:
                    db.add(Product(id=product_id,business_id=identifier,branch_id=identifier,category_id=category.id,
                        sku=f"SYN-{product_id}",name=name,price=price,available=available,
                        service_channels=["pos","digital_delivery","digital_takeaway"]))
                db.add(IntegrationCredential(business_id=identifier,branch_id=identifier,name="Agente sintético",
                    profile="restaurant_agent",token_prefix=f"smoke-{identifier}",token_hash=hash_integration_token(tokens[identifier]),
                    scopes=sorted(RESTAURANT_AGENT_SCOPES)))
        control_state.update(case_id=case_id,reset_count=control_state["reset_count"]+1)
        return {"synthetic_only":True,"database":"sqlite_memory","case_id":case_id,"businesses":[2,3],
            "real_whatsapp":False,"physical_printing":False,"printer_jobs_are_pending_only":True,
            "reset_count":control_state["reset_count"]}

    def serialize(value):
        if isinstance(value,Decimal):return float(value)
        if hasattr(value,"isoformat"):return value.isoformat()
        if isinstance(value,dict):return {k:("[REDACTED]" if k in ("lease_token","access_token","header_value") else serialize(v)) for k,v in value.items()}
        if isinstance(value,list):return [serialize(v) for v in value]
        return value

    def snapshot(business,sender):
        assert business in (2,3) and sender.isdigit() and 8<=len(sender)<=15
        with SessionLocal() as db:
            orders=list(db.scalars(select(Order).where(Order.business_id==business,Order.whatsapp_chat_id==sender)))
            order_ids=[o.id for o in orders]
            result={"synthetic_only":True,"database":"sqlite_memory","business_id":business,"sender":sender,
                "case_id":control_state["case_id"],"orders":[],"physical_printing":False}
            for row in orders:
                entry={column.name:getattr(row,column.name) for column in row.__table__.columns}
                entry["items"]=[{column.name:getattr(item,column.name) for column in item.__table__.columns} for item in row.items]
                result["orders"].append(entry)
            for name,model in [("payments",Payment),("tickets",KitchenTicket),("print_jobs",PrintJob),
                ("payment_requests",OrderPaymentRequest),("payment_evidence",PaymentEvidence),("delivery_assignments",DeliveryAssignment)]:
                rows=list(db.scalars(select(model).where(model.business_id==business,model.order_id.in_(order_ids)))) if order_ids else []
                result[name]=[{column.name:getattr(row,column.name) for column in row.__table__.columns} for row in rows]
            conversations=list(db.scalars(select(AgentConversation).where(AgentConversation.business_id==business,AgentConversation.sender==sender)))
            result["conversations"]=[{column.name:getattr(row,column.name) for column in row.__table__.columns} for row in conversations]
            conversation_ids=[c.id for c in conversations]
            for name,model in [("deliveries",AgentMessageDelivery),("model_usage",AgentModelUsage)]:
                rows=list(db.scalars(select(model).where(model.business_id==business,model.conversation_id.in_(conversation_ids)))) if conversation_ids else []
                result[name]=[{column.name:getattr(row,column.name) for column in row.__table__.columns} for row in rows]
            result["attention"]=[{column.name:getattr(row,column.name) for column in row.__table__.columns}
                for row in db.scalars(select(AgentAttentionRequest).where(AgentAttentionRequest.business_id==business,AgentAttentionRequest.sender==sender))]
            return serialize(result)

    seed("unstarted")
    client=TestClient(app)
    return {"client":client,"engine":engine,"tokens":tokens,"control":control,"seed":seed,"snapshot":snapshot,"state":control_state,"api_root":api_root}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--serve",action="store_true")
    parser.add_argument("--binding-file",type=Path)
    parser.add_argument("--port",type=int,default=0)
    parser.add_argument("--listen-host",choices=("127.0.0.1","0.0.0.0"),default="127.0.0.1")
    parser.add_argument("--container-isolation",action="store_true")
    parser.add_argument("--advertise-host",default="127.0.0.1")
    args=parser.parse_args()
    if not args.serve:
        print(json.dumps({"state":"INERT_NOT_STARTED","network":0,"models":0,"physical_printing":False}));return
    assert args.binding_file and args.binding_file.is_absolute() and not args.binding_file.exists()
    assert 0<=args.port<=65535
    if args.listen_host=="0.0.0.0":
        assert args.container_isolation and Path("/.dockerenv").is_file()
        assert os.environ.get("ESCALAR_QR_SANDBOX_CONTAINER")=="1"
        assert args.advertise_host=="impulsa-qr-api-eval" and args.port==8769
    else:
        assert not args.container_isolation and args.advertise_host=="127.0.0.1"
    sandbox=create_sandbox()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send_json(self,status,value):
            data=json.dumps(value,ensure_ascii=False).encode("utf8")
            self.send_response(status);self.send_header("content-type","application/json");self.send_header("content-length",str(len(data)));self.end_headers();self.wfile.write(data)
        def handle_request(self):
            try:
                size=int(self.headers.get("content-length","0"));assert 0<=size<=2*1024*1024
                raw=self.rfile.read(size) if size else b""
                parsed=urlsplit(self.path)
                if parsed.path.startswith("/control/"):
                    if not secrets.compare_digest(self.headers.get("X-Evaluation-Control",""),sandbox["control"]):self.send_json(401,{"code":"CONTROL_AUTH"});return
                    if parsed.path=="/control/start-case" and self.command=="POST":
                        body=json.loads(raw);assert isinstance(body.get("case_id"),str) and len(body["case_id"])<120
                        self.send_json(200,sandbox["seed"](body["case_id"]));return
                    if parsed.path=="/control/snapshot" and self.command=="GET":
                        q=parse_qs(parsed.query);self.send_json(200,sandbox["snapshot"](int(q["business_id"][0]),q["sender"][0]));return
                    if parsed.path=="/control/stop" and self.command=="POST":
                        self.send_json(200,{"state":"STOPPING"});threading.Thread(target=self.server.shutdown,daemon=True).start();return
                    self.send_json(404,{"code":"CONTROL_ROUTE"});return
                if not parsed.path.startswith("/api/v1/") and parsed.path not in ("/health","/openapi.json"):
                    self.send_json(404,{"code":"SANDBOX_ROUTE"});return
                # Never proxy to any host: TestClient dispatches directly to the
                # imported app, with the original HTTP status and bytes intact.
                headers={k:v for k,v in self.headers.items() if k.lower() not in ("host","content-length","connection")}
                response=sandbox["client"].request(self.command,self.path,headers=headers,content=raw)
                self.send_response(response.status_code)
                for key in ("content-type","x-request-id","cache-control"):
                    if key in response.headers:self.send_header(key,response.headers[key])
                self.send_header("content-length",str(len(response.content)));self.end_headers();self.wfile.write(response.content)
            except Exception as error:
                self.send_json(500,{"code":"SYNTHETIC_SANDBOX_ERROR","error_type":type(error).__name__})
        do_GET=handle_request
        do_POST=handle_request
        do_PATCH=handle_request
        do_PUT=handle_request
        do_DELETE=handle_request
    server=HTTPServer((args.listen_host,args.port),Handler)
    binding={"schema":"escalarai.qr-api-sandbox-binding.v1","synthetic_only":True,
        "base_url":f"http://{args.advertise_host}:{server.server_port}","api_base_path":"/api/v1",
        "control_token":sandbox["control"],"agent_tokens":{str(k):v for k,v in sandbox["tokens"].items()},
        "api_source":str(sandbox["api_root"]),"api_version":"1.1.0","database":"sqlite_memory",
        "container_isolation":args.container_isolation,
        "real_whatsapp":False,"physical_printing":False}
    args.binding_file.parent.mkdir(parents=True,exist_ok=True)
    with args.binding_file.open("x",encoding="utf8") as handle:json.dump(binding,handle,ensure_ascii=False,indent=2)
    print(json.dumps({"state":"SYNTHETIC_SANDBOX_LISTENING","url":binding["base_url"],"binding_file":str(args.binding_file),"secrets_printed":False}),flush=True)
    try:
        with sandbox["client"]:server.serve_forever(poll_interval=0.1)
    finally:server.server_close();sandbox["engine"].dispose()

if __name__=="__main__":main()
