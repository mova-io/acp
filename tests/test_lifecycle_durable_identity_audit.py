"""Offline lifecycle audit regressions; provider transports are existing fixture fakes."""
import pytest

from test_archive_execution import FakeGraph, OWNER, _seed, _run
from test_lifecycle_execution import gated_client, drive


def test_verified_autofire_projects_terminal_inventory(isolated_store):
    _seed(isolated_store)
    result = _run(isolated_store, FakeGraph())
    assert result['completed'] == 1
    assert isolated_store.get_lifecycle_status('s1', 'Clinical-Access-v2.docx')['lifecycle_status'] == 'Archived'


@pytest.mark.parametrize('terminal', ['Archived', 'Deleted', 'Already archived'])
def test_terminal_inventory_survives_changed_winning_rule(isolated_store, monkeypatch, terminal):
    import core, handlers
    from test_discover_lifecycle_rules import _policy
    st = isolated_store
    monkeypatch.setattr(core, 'store', st)
    st.init_scan_run('terminal-scan', 'drive', 1, '2026-09-15', 'default', 'r', owner=OWNER)
    st.add_inventory('terminal-scan', [{'file': 'a.docx', 'path': '/estate/a.docx', 'drive_file_id': 'id-a'}])
    st.set_lifecycle_status('terminal-scan', 'a.docx', terminal)
    _policy(st, 'new-delete', 'delete', [], owner=OWNER)
    handlers._evaluate_discover_lifecycle_rules('terminal-scan', 'drive', OWNER)
    assert st.get_lifecycle_status('terminal-scan', 'a.docx')['lifecycle_status'] == terminal


def test_graph_identity_cannot_resolve_as_google_drive(isolated_store, monkeypatch):
    import core
    import routes.disposition as rd
    monkeypatch.setattr(core, 'store', isolated_store)
    _seed(isolated_store)
    assert rd._lifecycle_drive_doc('scan:s1:Clinical-Access-v2.docx', OWNER) is None


def test_already_archived_is_excluded_from_default_assessment(isolated_store):
    assert 'Already archived' in isolated_store.LIFECYCLE_EXCLUDED_DEFAULT


def inventory(st, scan, *, owner=OWNER, provider='sharepoint', namespace='drive-a', item_id='item-a', file='a.docx', checked_out_by=None):
    st.init_scan_run(scan,provider,1,'2026-09-15','default','r',owner=owner,status='discovered')
    st.add_inventory(scan,[{'file':file,'path':'/estate/'+file,'drive_file_id':item_id,
        'drive_id':namespace if provider in ('sharepoint','onedrive') else None,
        'drive_account_id':namespace if provider=='drive' else None,'checked_out_by':checked_out_by}])


@pytest.mark.parametrize('provider',['drive','sharepoint','onedrive'])
@pytest.mark.parametrize('terminal',['Archived','Deleted','Already archived'])
def test_new_scan_and_renamed_file_retain_exact_source_terminal_state(isolated_store, provider, terminal):
    st=isolated_store
    inventory(st,'old',provider=provider)
    st.set_lifecycle_status('old','a.docx',terminal)
    inventory(st,'new',provider=provider,file='renamed.docx')
    assert st.get_lifecycle_status('new','renamed.docx')['lifecycle_status']==terminal


@pytest.mark.parametrize('difference',['tenant','provider','namespace','opaque_namespace_case','item'])
def test_same_name_or_item_id_does_not_cross_identity_boundaries(isolated_store,difference):
    st=isolated_store
    inventory(st,'old')
    st.set_lifecycle_status('old','a.docx','Deleted')
    fields={'tenant':{'owner':'other@example.com'},'provider':{'provider':'onedrive'},
        'namespace':{'namespace':'drive-b'},'opaque_namespace_case':{'namespace':'DRIVE-A'},'item':{'item_id':'item-b'}}[difference]
    inventory(st,'new',**fields)
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Active'


def test_tenant_and_provider_alias_case_normalize_but_opaque_account_does_not(isolated_store):
    st=isolated_store
    inventory(st,'old',owner='  Owner@Example.Com  ',provider=' SharePoint ')
    # Seed exact opaque identity because the fixture itself uses canonical provider selectors.
    with st._db.cursor() as cur:
        st._db.execute(cur,"UPDATE scan_inventory SET drive_id='drive-a' WHERE scan_id='old'")
    st.set_lifecycle_status('old','a.docx','Deleted')
    inventory(st,'new')
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Deleted'


@pytest.mark.parametrize('provider',['drive','sharepoint','onedrive','local'])
def test_missing_account_identity_never_links_by_filename(isolated_store,provider):
    st=isolated_store
    inventory(st,'old',provider=provider,namespace=None)
    st.set_lifecycle_status('old','a.docx','Deleted')
    st.set_lifecycle_status('old','a.docx','Active')
    assert st.get_lifecycle_status('old','a.docx')['lifecycle_status']=='Deleted'
    inventory(st,'new',provider=provider,namespace=None)
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Active'


def test_rule_reevaluation_retains_terminal_effective_projection(isolated_store,monkeypatch):
    import core,handlers
    from test_discover_lifecycle_rules import _policy
    st=isolated_store
    monkeypatch.setattr(core,'store',st)
    inventory(st,'old')
    _policy(st,'archive','archive',[],owner=OWNER)
    handlers._evaluate_discover_lifecycle_rules('old','sharepoint',OWNER)
    st.set_lifecycle_status('old','a.docx','Archived')
    _policy(st,'delete','delete',[],owner=OWNER)
    handlers._evaluate_discover_lifecycle_rules('old','sharepoint',OWNER)
    with st._db.cursor() as cur:
        st._db.execute(cur,"SELECT lifecycle_status,approval_status FROM effective_disposition WHERE scan_id='old'")
        state=st._db.fetchone(cur)
    assert state=={'lifecycle_status':'Archived','approval_status':'applied'}


def test_deleted_identity_survives_delta_absence_and_reappearance(isolated_store):
    import scanner
    st=isolated_store
    inventory(st,'old')
    st.set_lifecycle_status('old','a.docx','Deleted')
    raw={'id':'item-a','name':'a.docx','parentReference':{'driveId':'drive-a'}}
    assert scanner.apply_sp_delta([raw],[],{('drive-a','item-a')})==[]
    assert scanner.apply_sp_delta([],[raw],set())==[raw]
    inventory(st,'new')
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Deleted'


def test_checkout_metadata_is_not_cleared_by_relisting_or_rule_evaluation(isolated_store,monkeypatch):
    import core,handlers
    from test_discover_lifecycle_rules import _policy
    st=isolated_store
    monkeypatch.setattr(core,'store',st)
    inventory(st,'old',checked_out_by='author@example.com')
    inventory(st,'old',checked_out_by=None)
    _policy(st,'archive','archive',[],owner=OWNER)
    handlers._evaluate_discover_lifecycle_rules('old','sharepoint',OWNER)
    assert st.list_inventory('old')[0]['checked_out_by']=='author@example.com'


def test_checkout_live_preflight_blocks_automatic_archive(isolated_store):
    _seed(isolated_store)
    graph=FakeGraph(hold={'CheckoutUser':'author@example.com','_IsRecord':False})
    result=_run(isolated_store,graph)
    assert result['completed']==0 and graph.patched==[]
    assert 'checked out' in result['executions'][0]['detail']


def test_verified_restore_changes_future_scan_and_replay_does_not_undo_new_deletion(gated_client,isolated_store,drive):
    st=isolated_store
    inventory(st,'old',provider='drive',namespace='account-a')
    st.create_disposition_policy('rule',name='rule',match='[]',action='delete',action_config='{}',
        requires_approval=True,enabled=True,owner_email=OWNER)
    st.create_disposition_audit('audit',doc_id='scan:old:a.docx',policy_id='rule',action='delete',
        result='pending_approval',detail='candidate',owner_email=OWNER)
    client=gated_client(OWNER)
    assert client.post('/disposition/approvals/audit/approve').status_code==200
    assert st.get_lifecycle_status('old','a.docx')['lifecycle_status']=='Deleted'
    st.set_lifecycle_status('old','a.docx','Deleted',exclusion_reason='excluded from Assess')
    assert client.post('/disposition/approvals/audit/undo').status_code==200
    assert drive._files.state['trashed'] is False
    inventory(st,'restored',provider='drive',namespace='account-a')
    assert st.get_lifecycle_status('restored','a.docx')['lifecycle_status']=='Active'
    st.set_lifecycle_status('restored','a.docx','Deleted',evidence_id='new-delete')
    drive._files.state['trashed']=True
    calls=len(drive._files.touched)
    assert client.post('/disposition/approvals/audit/undo').status_code==200
    assert drive._files.state['trashed'] is True and len(drive._files.touched)==calls
    inventory(st,'new',provider='drive',namespace='account-a')
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Deleted'


def governance(st, *, unattended=False, action='delete'):
    st.upsert_document('drive:item-a',source='drive',path='/estate/a.docx',content_hash=None,owner=OWNER,
        created_at=None,last_seen=None,triage_score=None,triage_rationale=None,owner_email=OWNER)
    st.create_disposition_policy('governance',name='governance',match='[]',action=action,action_config='{"target_folder_id":"archive"}',
        requires_approval=not unattended,enabled=True,owner_email=OWNER)
    if not unattended:
        st.create_disposition_audit('governance-audit',doc_id='drive:item-a',policy_id='governance',
            action=action,result='pending_approval',detail='candidate',owner_email=OWNER)


@pytest.mark.parametrize('unattended',[False,True])
@pytest.mark.parametrize('action',['archive','delete'])
def test_governance_archive_delete_execution_and_restore_project_source_state(gated_client,isolated_store,drive,unattended,action):
    st=isolated_store
    inventory(st,'old',provider='drive',namespace='account-a')
    inventory(st,'known',provider='drive',namespace='account-a',file='renamed.docx')
    governance(st,unattended=unattended,action=action)
    client=gated_client(OWNER)
    url='/disposition/policies/governance/execute' if unattended else '/disposition/approvals/governance-audit/approve'
    response=client.post(url)
    assert response.status_code==200,response.text
    terminal='Deleted' if action=='delete' else 'Archived'
    assert st.get_lifecycle_status('old','a.docx')['lifecycle_status']==terminal
    assert st.get_lifecycle_status('known','renamed.docx')['lifecycle_status']==terminal
    rows=st.list_disposition_audit(owner=OWNER)
    receipt=next(r for r in rows if r['action']==action and r['result']=='applied')
    assert client.post(f"/disposition/approvals/{receipt['id']}/undo").status_code==200
    inventory(st,'new',provider='drive',namespace='account-a')
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Active'


def test_governance_action_refuses_ambiguous_account_namespace(gated_client,isolated_store,drive):
    st=isolated_store
    inventory(st,'one',provider='drive',namespace='account-a')
    inventory(st,'two',provider='drive',namespace='account-b')
    governance(st)
    response=gated_client(OWNER).post('/disposition/approvals/governance-audit/approve')
    assert response.status_code==409,response.text
    assert drive._files.touched==[]


def test_old_undo_cannot_restore_a_newer_terminal_decision(gated_client,isolated_store,drive):
    st=isolated_store
    inventory(st,'old',provider='drive',namespace='account-a')
    governance(st)
    client=gated_client(OWNER)
    assert client.post('/disposition/approvals/governance-audit/approve').status_code==200
    st.set_lifecycle_status('old','a.docx','Deleted',evidence_id='new-delete')
    calls=len(drive._files.touched)
    response=client.post('/disposition/approvals/governance-audit/undo')
    assert response.status_code==409,response.text
    assert len(drive._files.touched)==calls


def test_unverified_provider_restore_retains_terminal_exclusion(gated_client,isolated_store,drive,monkeypatch):
    from test_lifecycle_execution import _Exec
    st=isolated_store
    inventory(st,'old',provider='drive',namespace='account-a')
    governance(st)
    client=gated_client(OWNER)
    assert client.post('/disposition/approvals/governance-audit/approve').status_code==200
    monkeypatch.setattr(drive._files,'get',lambda **kwargs:_Exec({'trashed':True}))
    response=client.post('/disposition/approvals/governance-audit/undo')
    assert response.status_code==502,response.text
    assert st.get_lifecycle_status('old','a.docx')['lifecycle_status']=='Deleted'
    inventory(st,'new',provider='drive',namespace='account-a')
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Deleted'


@pytest.mark.parametrize('operation',['delete_scan','reset_analytics'])
def test_source_tombstone_survives_scan_history_pruning(isolated_store,operation):
    st=isolated_store
    inventory(st,'old')
    st.set_lifecycle_status('old','a.docx','Deleted')
    if operation=='delete_scan':
        st.delete_scan('old',OWNER)
    else:
        st.reset_analytics()
    inventory(st,'new')
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Deleted'


def test_missing_identity_is_reported_without_guessing_cross_scan_linkage(isolated_store):
    inventory(isolated_store,'old',provider='drive',namespace=None)
    isolated_store.set_lifecycle_status('old','a.docx','Deleted')
    assert isolated_store.lifecycle_summary('old',OWNER)['source_identity_linkage']['unavailable']==1


def test_two_matching_archive_delete_rules_produce_one_effective_recommendation(isolated_store,monkeypatch):
    import core,handlers
    from test_discover_lifecycle_rules import _policy
    st=isolated_store
    monkeypatch.setattr(core,'store',st)
    inventory(st,'old')
    _policy(st,'archive','archive',[],owner=OWNER)
    _policy(st,'delete','delete',[],owner=OWNER)
    handlers._evaluate_discover_lifecycle_rules('old','sharepoint',OWNER)
    assert st.get_lifecycle_status('old','a.docx')['lifecycle_status']=='Archive Candidate'
    summary=st.lifecycle_summary('old',OWNER)
    assert summary['counts']['archive_candidate']==1 and summary['counts']['delete_candidate']==0
    assert summary['reconciled_total']==1


def test_equal_priority_archive_delete_conflict_stays_nonterminal(isolated_store,monkeypatch):
    import core,handlers
    from test_discover_lifecycle_rules import _policy
    st=isolated_store
    monkeypatch.setattr(core,'store',st)
    inventory(st,'old')
    _policy(st,'archive','archive',[],owner=OWNER)
    _policy(st,'delete','delete',[],owner=OWNER)
    with st._db.cursor() as cur:
        st._db.execute(cur,'UPDATE disposition_policy SET priority=1')
    handlers._evaluate_discover_lifecycle_rules('old','sharepoint',OWNER)
    assert st.get_lifecycle_status('old','a.docx')['lifecycle_status']=='Conflict — review required'
    assert st.list_disposition_audit(result='pending_approval',owner=OWNER)==[]
    inventory(st,'new')
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Active'


def test_late_recommendation_cannot_overwrite_terminal_projection(isolated_store):
    st=isolated_store
    inventory(st,'old')
    st.set_lifecycle_status('old','a.docx','Deleted')
    st.bulk_upsert_effective_dispositions([('a.docx','old','stale-evaluation','Archive Candidate',
        'late rule','pending_approval',None,st._now(),OWNER)])
    st.bulk_set_lifecycle_status([('old','a.docx','Archive Candidate','stale-rule','late rule')])
    with st._db.cursor() as cur:
        st._db.execute(cur,"SELECT lifecycle_status,approval_status FROM effective_disposition WHERE scan_id='old'")
        projection=st._db.fetchone(cur)
    assert projection=={'lifecycle_status':'Deleted','approval_status':'applied'}
    assert st.get_lifecycle_status('old','a.docx')['lifecycle_status']=='Deleted'


def test_inventory_filename_rebinding_cannot_restore_another_source_item(gated_client,isolated_store,drive):
    st=isolated_store
    inventory(st,'old',provider='drive',namespace='account-a')
    governance(st)
    client=gated_client(OWNER)
    assert client.post('/disposition/approvals/governance-audit/approve').status_code==200
    # Simulate legacy/out-of-band corruption, bypassing the guarded inventory upsert.
    with st._db.cursor() as cur:
        st._db.execute(cur,"UPDATE scan_inventory SET drive_file_id='different-item' WHERE scan_id='old'")
    calls=len(drive._files.touched)
    response=client.post('/disposition/approvals/governance-audit/undo')
    assert response.status_code==409,response.text
    assert len(drive._files.touched)==calls


def test_plain_reactivated_status_does_not_clear_provider_deletion(isolated_store):
    st=isolated_store
    inventory(st,'old')
    st.set_lifecycle_status('old','a.docx','Deleted')
    st.set_lifecycle_status('old','a.docx','Reactivated')
    assert st.get_lifecycle_status('old','a.docx')['lifecycle_status']=='Deleted'


def test_protected_inventory_rebinding_is_rejected_and_counted(isolated_store):
    st=isolated_store
    inventory(st,'old')
    st.set_lifecycle_status('old','a.docx','Deleted')
    result=st.add_inventory('old',[{'file':'a.docx','drive_file_id':'different-item','drive_id':'drive-a'}])
    assert result['failed']==1 and result['updated']==0
    assert st.list_inventory('old')[0]['drive_file_id']=='item-a'
    inventory(st,'new',item_id='different-item')
    assert st.get_lifecycle_status('new','a.docx')['lifecycle_status']=='Active'
