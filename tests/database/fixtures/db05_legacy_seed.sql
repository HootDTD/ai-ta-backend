-- Deterministic, scrubbed DB-05 rehearsal data. Local Docker only.
-- Populates every one of the 42 legacy tables, both sides of each merge,
-- nullable/non-null JSON branches, the FK web, and a 3072-dimensional vector.

INSERT INTO auth.users (id) VALUES
 ('10000000-0000-4000-8000-000000000001'),
 ('20000000-0000-4000-8000-000000000002')
ON CONFLICT DO NOTHING;

INSERT INTO aita_search_spaces
 (id,name,slug,subject_name,weight_overrides,metadata,created_at,updated_at)
VALUES
 (1,'DB05 Physics','db05-physics','Physics','{"semantic":0.7}',
  '{"fixture":true}','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
INSERT INTO teacher_courses
 (id,search_space_id,current_week,weights,weight_bounds,created_at,updated_at)
VALUES
 (2,1,3,'{"keyword":0.3}','{"min":0.1,"max":0.9}',
  '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
INSERT INTO course_memberships
 (user_id,search_space_id,role,created_at,updated_at)
VALUES
 ('10000000-0000-4000-8000-000000000001',1,'student','2026-01-01','2026-01-01'),
 ('20000000-0000-4000-8000-000000000002',1,'teacher','2026-01-01','2026-01-01');
INSERT INTO course_invite_links
 (id,code,search_space_id,role,created_by,is_active,max_uses,use_count,created_at,updated_at)
VALUES
 (3,'DB05INVITE',1,'student','20000000-0000-4000-8000-000000000002',true,5,1,'2026-01-01','2026-01-01');

INSERT INTO aita_documents
 (id,title,document_type,material_kind,content,source_markdown,content_hash,
  unique_identifier_hash,embedding,document_metadata,page_count,week,status,
  search_space_id,created_at,updated_at)
VALUES
 (10,'Seed document','EDUCATIONAL_FILE','notes','Bernoulli content','# Bernoulli',
  'db05-doc-hash','db05-doc-unique',
  ('[' || array_to_string(array_fill('0.001'::text,ARRAY[3072]),',') || ']')::extensions.vector,
  '{"provider":"fixture"}',2,3,'{"state":"failed","reason":"fixture branch"}',
  1,'2026-01-01','2026-01-01');
INSERT INTO aita_chunks
 (id,content,embedding,page_number,section_path,chunk_type,figure_id,document_id,created_at)
VALUES
 (11,'Seed chunk',
  ('[' || array_to_string(array_fill('0.002'::text,ARRAY[3072]),',') || ']')::extensions.vector,
  1,'section','body',NULL,10,'2026-01-01');
INSERT INTO teacher_uploads
 (id,search_space_id,week,kind,title,source_name,doc_id,page_count,is_latest,
  uploaded_by,metadata,uploaded_at,created_at,updated_at,status,storage_key,
  artifact_manifest,ocr_provider,ocr_summary,warning_count,error_message,
  started_at,completed_at,attempt_count)
VALUES
 (12,1,3,'notes','Seed upload','seed.pdf',10,2,true,
  '20000000-0000-4000-8000-000000000002','{"nullable_branch":false}',
  '2026-01-01','2026-01-01','2026-01-01','ready','db05/seed.pdf',
  '{"pages":[1,2]}','fixture','{"confidence":0.8}',1,NULL,
  '2026-01-01','2026-01-01',1);
INSERT INTO teacher_upload_jobs
 (id,upload_id,state,lease_owner,lease_expires_at,attempt_count,last_error,created_at,updated_at)
VALUES (13,12,'completed',NULL,NULL,1,NULL,'2026-01-01','2026-01-01');

INSERT INTO chat_sessions
 (id,chat_id,user_id,search_space_id,meta,memory_summary,created_at,updated_at)
VALUES
 (20,'db05-chat','10000000-0000-4000-8000-000000000001',1,
  '{"mode":"study"}','fixture summary','2026-01-01','2026-01-01');
INSERT INTO chat_turns
 (id,chat_session_id,turn_index,turn_id,role,content,created_at,model,
  tool_name,tool_inputs,attachments,citations,keywords)
VALUES
 (21,20,0,'db05-turn','user','Explain Bernoulli','2026-01-01',NULL,NULL,
  '{"depth":1}','[{"name":"notes.pdf"}]','[{"document_id":10}]',
  '["bernoulli","pressure"]');
INSERT INTO chat_session_snippets
 (chat_session_id,chunk_id,original_score,first_seen_turn,last_used_turn,snippet_payload,created_at)
VALUES (20,11,0.8,0,1,'{"snippet":{"text":"Seed chunk"}}','2026-01-01');
INSERT INTO chat_router_decisions
 (id,turn_id,chat_session_id,query,stage1_top1,stage1_margin,stage1_route,
  stage2_invoked,stage2_route,stage2_confidence,retrieval_mode,
  retrieval_top_score,final_route,was_clarified,clarify_cause,
  latency_router_ms,latency_retrieval_ms,latency_answer_ms,created_at)
OVERRIDING SYSTEM VALUE
VALUES
 (22,'db05-turn',20,'Explain Bernoulli',0.9,0.4,'retrieve',true,'retrieve',
  0.95,'hybrid',0.88,'retrieve',false,NULL,5,7,11,'2026-01-01');

INSERT INTO apollo_subjects (id,slug,display_name,created_at,search_space_id)
OVERRIDING SYSTEM VALUE VALUES (30,'physics','Physics','2026-01-01',1);
INSERT INTO apollo_concepts
 (id,subject_id,slug,display_name,canonical_symbols,normalization_map,
  parser_prompt_template,solver_hints,forbidden_named_laws,concept_dag,
  created_at,updated_at,description)
OVERRIDING SYSTEM VALUE
VALUES
 (31,30,'bernoulli','Bernoulli principle',
  '{"symbols":["p","v"],"convention":"SI"}','{"P":"p"}','Parse {{text}}',
  '{"method":"energy"}','["named shortcut"]','{"p":["v"]}',
  '2026-01-01','2026-01-01','Energy along a streamline');
INSERT INTO apollo_concept_problems
 (id,concept_id,problem_code,difficulty,payload,created_at,tier,solution_source,
  provenance,quarantined_at,search_space_id)
OVERRIDING SYSTEM VALUE
VALUES
 (32,31,'db05-problem','standard',
  '{"problem_text":"Find the pressure","given_values":{"v":2},"target_unknown":"p","reference_solution":{"steps":[{"id":"s1"}]},"future_key":"kept"}',
  '2026-01-01',2,'authored','{"document_id":10}',NULL,1);

INSERT INTO apollo_sessions
 (id,status,phase,current_problem_id,created_at,last_touched_at,pending_intent,
  history_summary,history_summary_up_to_turn,concept_id,user_id,search_space_id)
VALUES
 (40,'active','SOLVING','db05-problem','2026-01-01','2026-01-01',NULL,
  'old turns',0,31,'10000000-0000-4000-8000-000000000001',1);
INSERT INTO apollo_problem_attempts
 (id,session_id,problem_id,difficulty,result,solver_trace,diagnostic_report,
  created_at,learner_update_pending,learner_update_attempts,
  learner_update_failed_at,learner_update_last_error,learner_update_next_attempt_at)
VALUES
 (41,40,'db05-problem','standard','graded','{"steps":[]}',
  '{"score":0.8}','2026-01-01',false,1,NULL,NULL,NULL);
INSERT INTO apollo_messages
 (id,session_id,role,content,turn_index,created_at,attempt_id,metadata)
VALUES
 (42,40,'student','I think pressure rises',0,'2026-01-01',41,
  '{"low_conf_pattern":true,"intent":"answer","olm_invite":"later","misconception":"drop"}');
INSERT INTO apollo_student_progress (user_id,xp_total,level,last_level_up_at)
VALUES ('10000000-0000-4000-8000-000000000001',120,2,NULL);
INSERT INTO ai_use_reports
 (id,chat_id,created_at,style,length,markdown,jsonld,model_fingerprint,tool_calls,prompt_hashes)
VALUES
 ('50000000-0000-4000-8000-000000000005','db05-chat','2026-01-01','academic','short',
  '# Report','{"@type":"Report"}','fixture-model','[{"name":"search"}]','["hash-1"]');

INSERT INTO apollo_reference_question_opportunities
 (id,attempt_id,session_id,reference_node_id,state,question,asked_turn,answered_turn,created_at)
VALUES (50,41,40,'node-a','answered','Why?',1,2,'2026-01-01');
INSERT INTO apollo_question_tally
 (id,attempt_id,reference_node_id,status,evidence,student_declined,times_asked,last_asked_turn,updated_at)
VALUES
 (51,41,'node-a','answered','[{"turn":2}]',false,1,1,'2026-01-01'),
 (52,41,'node-tally-only','available','[{"turn":3}]',true,2,3,'2026-01-01');

INSERT INTO apollo_kg_entities
 (id,concept_id,canonical_key,kind,display_name,payload,aliases,created_at,updated_at,scope_summary)
OVERRIDING SYSTEM VALUE
VALUES
 (60,31,'bernoulli/pressure','variable','Pressure','{"symbol":"p"}','["P"]','2026-01-01','2026-01-01','pressure variable'),
 (61,31,'bernoulli/equation','equation','Bernoulli equation','{"latex":"p+rho"}','["energy equation"]','2026-01-01','2026-01-01','energy equation');
INSERT INTO apollo_entity_prereqs (from_entity_id,to_entity_id) VALUES (60,61);
INSERT INTO apollo_learner_state
 (user_id,search_space_id,entity_id,belief,mastery,confidence,
  misconception_code,evidence_count,last_evidence_at,updated_at)
VALUES
 ('10000000-0000-4000-8000-000000000001',1,60,ARRAY[0.1,0.2,0.7]::real[],
  0.7,0.8,NULL,2,'2026-01-01','2026-01-01');
INSERT INTO apollo_mastery_events
 (id,user_id,search_space_id,entity_id,attempt_id,event_kind,score,
  misconception_code,parser_confidence,grader_confidence,negotiation_move,
  reference_step_id,prior_belief,posterior_belief,mastery_after,
  dt_days_since_last,evidence_node_ids,created_at,concept_problem_id)
OVERRIDING SYSTEM VALUE
VALUES
 (62,'10000000-0000-4000-8000-000000000001',1,60,41,'covered',0.8,NULL,
  0.9,0.85,'paraphrase','s1',ARRAY[0.2,0.3,0.5]::real[],
  ARRAY[0.1,0.2,0.7]::real[],0.7,1.0,'["neo4j-1"]','2026-01-01',32);
INSERT INTO apollo_kg_negotiations
 (id,attempt_id,entry_id,actor,move,payload,created_at)
OVERRIDING SYSTEM VALUE
VALUES (63,41,'neo4j-1','student','paraphrase','{"text":"my words"}','2026-01-01');

INSERT INTO apollo_ingest_runs
 (id,search_space_id,document_id,content_hash,status,n_questions_scraped,
  n_promoted,n_rejected,n_dedup_merged,llm_calls,llm_tokens_in,llm_tokens_out,
  llm_cost_usd,started_at,finished_at,created_at,n_pages,dedup_pressure)
OVERRIDING SYSTEM VALUE
VALUES
 (70,1,10,'db05-doc-hash','succeeded',3,1,1,1,2,100,50,0.012345,
  '2026-01-01','2026-01-01','2026-01-01',2,
  '{"total_candidates":3,"exact_merges":1,"embedding_merges":1,"embedding_distinct":1,"embedding_merge_ratio":0.5,"per_concept":{"bernoulli":1}}');
INSERT INTO apollo_ingest_errors
 (id,ingest_run_id,search_space_id,stage,error_class,context,created_at)
OVERRIDING SYSTEM VALUE
VALUES (71,70,1,'scrape','FixtureError','{"page":2}','2026-01-01');
INSERT INTO apollo_ingest_page_evidence
 (id,ingest_run_id,search_space_id,document_id,role,page_number,ocr_text,
  ocr_confidence,extraction_mode,verify_path_fired,created_at)
OVERRIDING SYSTEM VALUE
VALUES (72,70,1,10,'problem',1,'Find pressure',0.55,'ocr',true,'2026-01-01');
INSERT INTO apollo_dedup_decisions
 (id,ingest_run_id,search_space_id,concept_id,candidate_key,method,
  similarity,verdict,matched_entity_id,created_at)
OVERRIDING SYSTEM VALUE
VALUES (73,70,1,31,'bernoulli/pressure','embedding',0.93,'merged',60,'2026-01-01');
INSERT INTO apollo_generation_runs
 (id,search_space_id,concept_id,status,result_summary,ingest_run_id,created_at)
VALUES (74,1,31,'succeeded','{"created":1}',70,'2026-01-01');
INSERT INTO apollo_authored_sets
 (id,search_space_id,set_index,problem_document_id,solution_document_id,status,
  result_summary,created_at,updated_at)
VALUES (75,1,0,10,NULL,'done','{"paired":1}','2026-01-01','2026-01-01');

INSERT INTO apollo_grading_artifacts
 (id,attempt_id,role,grader_used,user_id,search_space_id,concept_id,problem_id,
  versions,node_ledger,edge_ledger,misconceptions,clarification_trace,scores,
  abstention,grading_latency_ms,created_at)
VALUES
 (80,41,'canonical','graph','10000000-0000-4000-8000-000000000001',1,31,
  'db05-problem','{"grader":"graph-v1","reference_graph_hash":"ref-1","weights_version":"w1"}',
  '[{"key":"n1"}]','[{"key":"e1"}]','[]','[]',
  '{"composite":0.8,"node_coverage":0.9,"edge_coverage":0.7}',NULL,15,'2026-01-01');
INSERT INTO apollo_graph_comparison_runs
 (id,attempt_id,user_id,search_space_id,coverage_score,soundness_score,
  bisimilarity_score,node_coverage_score,edge_coverage_score,scoping_score,
  usage_score,procedure_order_score,dependency_score,contradiction_score,
  normalization_confidence,abstained,abstention_reasons,comparison_version,
  reference_graph_hash,created_at,soundness_applicable)
OVERRIDING SYSTEM VALUE
VALUES
 (81,41,'10000000-0000-4000-8000-000000000001',1,0.8,0.9,0.85,0.8,0.75,
  0.9,0.8,0.7,0.9,0.1,0.95,false,'[]','shadow-v1','ref-1','2026-01-01',true);
INSERT INTO apollo_graph_comparison_findings
 (id,run_id,entity_id,finding_kind,score,confidence,student_node_ids,
  reference_node_ids,student_edge_ids,reference_edge_ids,evidence_spans,message,created_at)
OVERRIDING SYSTEM VALUE
VALUES
 (82,81,60,'covered_node',0.9,0.95,'["student-n1"]','["ref-n1"]','[]','[]',
  '[{"text":"pressure"}]','covered','2026-01-01');

-- The six intentionally uncopied tables are deliberately non-empty.
INSERT INTO apollo_kg_entries
 (id,session_id,type,content,source,created_at,attempt_id)
VALUES (90,40,'equation','{"latex":"p+rho"}','parser','2026-01-01',41);
INSERT INTO apollo_misconceptions
 (id,concept_id,code,description,trigger_phrases,probe_question,rt_steps,
  created_at,updated_at,opposes)
OVERRIDING SYSTEM VALUE
VALUES (91,31,'pressure_speed','Confuses pressure and speed','["faster means higher pressure"]',
        'What happens to pressure?','["compare terms"]','2026-01-01','2026-01-01','bernoulli/pressure');
INSERT INTO apollo_clarifications
 (id,attempt_id,session_id,user_id,search_space_id,concept_id,node_id,
  candidate_key,state,probe_question,original_statement,clarification_text,
  asked_turn,answered_turn,created_at,updated_at)
OVERRIDING SYSTEM VALUE
VALUES (92,41,40,'10000000-0000-4000-8000-000000000001',1,31,'node-a',
        'pressure_speed','confirmed','Do you mean pressure?','Pressure rises','Yes',1,2,
        '2026-01-01','2026-01-01');
INSERT INTO apollo_misconception_observations
 (id,search_space_id,concept_id,signature,user_id,attempt_id,confidence,
  opposes,evidence_span,source,created_at)
VALUES (93,1,31,'pressure_speed','10000000-0000-4000-8000-000000000001',41,
        0.9,'bernoulli/pressure','Pressure rises','grading_artifact','2026-01-01');
INSERT INTO apollo_provisioning_jobs
 (id,search_space_id,document_id,state,ingest_run_id,lease_owner,
  lease_expires_at,attempt_count,last_error,created_at,updated_at)
OVERRIDING SYSTEM VALUE
VALUES (94,1,10,'completed',70,NULL,NULL,1,NULL,'2026-01-01','2026-01-01');
INSERT INTO apollo_rejected_problems
 (id,ingest_run_id,search_space_id,concept_id,failed_gate,rejected_stage,
  diagnostic,payload,created_at)
OVERRIDING SYSTEM VALUE
VALUES (95,70,1,31,2,'promotion_lint','fixture rejection','{"reason":"bad units"}','2026-01-01');
