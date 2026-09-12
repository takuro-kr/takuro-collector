import json
import pytest
import requests
from takuro_collector.translation import translate, TranslationError

class Session:
    def __init__(self, mode="ok"):
        self.calls=[]; self.mode=mode
    def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        if self.mode=="timeout": raise requests.ReadTimeout()
        items=json.loads(kwargs["json"]["input"][1]["content"])
        rows=[dict(item,ko="테스트") for item in items]
        if self.mode=="missing": rows=[]
        class Response:
            status_code=200
            def json(self):return {"status":"completed","output":[{"content":[{"type":"output_text","text":json.dumps({"translations":rows})}]}]}
        return Response()

def test_chunking_and_exact_identity():
    items=[{"kind":"stations","ja":str(i)} for i in range(11)]
    session=Session()
    assert len(translate(items,"test-secret",session))==11
    assert len(session.calls)==3
    assert all(url=="https://api.openai.com/v1/responses" for url,kw in session.calls)
    assert all(kw["json"]["store"] is False for url,kw in session.calls)

def test_missing_rows_are_rejected():
    with pytest.raises(TranslationError):translate([{"kind":"stations","ja":"x"}],"secret",Session("missing"))

def test_timeout_does_not_claim_firewall_and_does_not_expose_key():
    with pytest.raises(TranslationError) as error:translate([{"kind":"stations","ja":"x"}],"secret-key",Session("timeout"))
    assert "secret-key" not in str(error.value)
    assert "확정할 수 없습니다" in str(error.value)


def test_failed_wordpress_delivery_reuses_cached_result(monkeypatch):
    import takuro_collector.translation as module
    class DB:
        values={"openai_translation_key":"protected"}
        def get_setting(self,k,default=""): return self.values.get(k,default)
        def set_setting(self,k,v): self.values[k]=v
    class Client:
        def __init__(self):self.session=self;self.deliveries=0
        def registration_api(self,path):return path
        def post(self,path,**kwargs):
            if path.endswith("claim"):return {"job":{"id":"j","token":"t","items":[{"kind":"stations","ja":"x"}]}}
            self.deliveries+=1
            if self.deliveries==1:raise requests.ReadTimeout()
            return {"ok":True}
        def _json(self,x):return x
    client=Client()
    class Sync:
        def __init__(self,db):pass
        def configured(self):return True
        def client(self):return client
    calls=[]
    monkeypatch.setattr(module,"WordPressSync",Sync)
    monkeypatch.setattr(module,"unprotect",lambda x:"secret")
    def fake_translate(*args,**kwargs):
        calls.append(1)
        return [{"kind":"stations","ja":"x","ko":"엑스"}]
    monkeypatch.setattr(module,"translate",fake_translate)
    db=DB()
    with pytest.raises(requests.ReadTimeout):module.process_job(db)
    assert db.values["translation_unsent"]
    assert module.process_job(db)["ok"]
    assert len(calls)==1
    assert db.values["translation_unsent"]==""
