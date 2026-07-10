# Graph alignment, edit distance & partial edge credit

Research memo, 2026-07-07. Angle: joint node+edge graph alignment (SMATCH lineage), graph edit
distance & soft graph matching, KG entity alignment, and partial-credit schemes for edges with
unresolved endpoints or paraphrased relations. Framing problem: Apollo's resolver freezes node
resolution BEFORE edges are scored, so edge_coverage compounds node-recall failures quadratically
(strong attempts resolve 1/5 nodes; edge_coverage caps at 0.25 over 31 attempts).

## Key findings

1. **Smatch (Cai & Knight 2013) is exactly the "unfrozen" version of our pipeline.** Smatch scores
   two semantic graphs by converting both to triples (instance, attribute, relation) and searching
   for a variable(node)-to-variable mapping that MAXIMIZES the number of matching triples; P/R/F1
   are computed over triples under that best mapping (https://amr.isi.edu/smatch-13.pdf,
   https://aclanthology.org/P13-2131/, https://github.com/snowblink14/smatch — MIT). Crucially,
   node identity is decided by the mapping (which is chosen to maximize total node+edge agreement),
   not by a per-node lexical gate: an edge between two candidate nodes can pull those nodes onto
   the reference nodes even when their labels alone would not resolve. This is the single most
   direct prior-art answer to our defect: node resolution and edge scoring should be one joint
   optimization, not a pipeline.

2. **Smatch++ (Opitz, EACL Findings 2023) makes the optimal alignment practical and standardized.**
   Hill-climbing Smatch has documented search errors; Smatch++ provides an ILP solver with
   optimality guarantees plus lossless graph compression, standardized preprocessing, and
   fine-grained subgraph scoring (https://arxiv.org/abs/2305.06993,
   https://github.com/flipz357/smatchpp — GPL-3.0, active, v1.8.0 May 2025, `pip install smatchpp`).
   Our reference graphs are ~5 nodes; exact ILP alignment is trivial at that size. Smatchpp also
   exposes **customizable triple-matching functions** for graded similarity ("cat" ~ "kitten").

3. **S2match (Opitz, Parcalabescu & Frank, TACL 2020, "AMR Similarity Metrics from Principles")
   adds graded node credit inside the alignment objective.** Concept-node matches are scored by
   embedding cosine with a threshold tau instead of string equality, giving partial credit for
   near-synonym labels while the mapping still maximizes global agreement
   (https://arxiv.org/abs/2001.10929, https://github.com/flipz357/amr-metric-suite — MIT). For us:
   the resolver's binary resolve/abstain becomes a similarity weight; a 0.7-similar node contributes
   0.7 node credit and keeps its edges alive instead of zeroing them.

4. **SPICE (Anderson et al., ECCV 2016) decomposes credit so edge failure never zeroes node
   credit.** Caption scene graphs are scored as F1 over a tuple set containing unary object tuples,
   attribute pairs, AND relation triples, with WordNet synonyms as positive matches
   (https://arxiv.org/abs/1607.08822, https://panderson.me/spice/). Because unary tuples are scored
   independently, a student who names the concepts but fumbles a relation still earns node credit —
   the "partial credit when only one endpoint resolves" pattern, institutionalized.

5. **SoftSPICE (Li et al. 2023, in FACTUAL, ACL Findings) scores whole triples as phrases —
   paraphrased-relation partial credit without endpoint resolution.** Each graph sub-component
   (object / object+attribute / subject-predicate-object) is rendered as text, embedded with
   Sentence-BERT, and triple-level cosine similarities are aggregated to a graph score
   (https://arxiv.org/abs/2305.17497, https://aclanthology.org/2023.findings-acl.398.pdf). For us:
   embed "pressure drops where velocity rises" and match it against the verbalized reference edge
   USES(bernoulli_equation, pressure_velocity_tradeoff) directly — the edge earns graded credit even
   when neither endpoint resolved individually.

6. **Rematch (Kachwala et al., NAACL Findings 2024) shows alignment-free motif overlap is a cheap
   strong baseline.** It decomposes AMRs into semantic motifs and takes Jaccard overlap; first on
   semantic similarity (STS-B/SICK-R), ~5x faster than the next metric
   (https://arxiv.org/abs/2404.02126, https://github.com/Zoher15/Rematch-RARE). Useful as a sanity
   metric, but motif overlap is symmetric similarity, not reference coverage — secondary for grading.

7. **Graph edit distance: exact is NP-complete; usable approximations exist but the semantics are
   wrong for grading.** Riesen & Bunke's bipartite approximation reduces GED to a linear sum
   assignment over a cost matrix of node substitution/insertion/deletion costs including local edge
   structure, cubic time (https://www.sciencedirect.com/science/article/abs/pii/S003132031400452X,
   https://bougleux.users.greyc.fr/articles/ged-prl.pdf). FGWAlign (Tang et al., VLDB 2025) casts
   GED as fused Gromov-Wasserstein optimization, cuts computation error >80% with 15-60x speedups,
   and — notably for us — extends to multi-relational graphs with edge labels
   (https://www.vldb.org/pvldb/vol18/p3641-tang.pdf, https://github.com/squareRoot3/FGWAlign).

8. **Fused Gromov-Wasserstein optimal transport = principled soft graph matching.** FGW (Vayer et
   al. 2019) jointly transports node features and graph structure (https://arxiv.org/abs/1805.09114);
   Gromov-Wasserstein Learning does graph matching + node embedding via OT
   (https://proceedings.mlr.press/v97/xu19b/xu19b.pdf). The transport plan is a soft many-to-many
   node correspondence with fractional masses — directly interpretable as fractional coverage.
   Mature implementation in the POT library (https://pythonot.github.io/, MIT).

9. **FGWEA (Tang et al., ACL Findings 2023): unsupervised FGW entity alignment beats 21 baselines
   including supervised ones.** Three stages: semantic embedding matching, then iterative
   structural/relational matching anchored on high-confidence links, then global structural
   comparison (https://aclanthology.org/2023.findings-acl.205/,
   https://github.com/squareRoot3/FusedGW-Entity-Alignment). This is our situation in miniature:
   anchor on the 1-2 confidently resolved nodes, then let structure propagate alignment to the rest.

10. **Embedding-based KG entity alignment (GCN-Align lineage) and its 2024-2026 LLM turn.**
    GCN-Align (EMNLP 2018, https://aclanthology.org/D18-1032/) started GNN-based EA; OpenEA
    benchmark (VLDB 2020, https://github.com/nju-websoft/OpenEA) systematized it. The current
    pattern (ChatEA https://arxiv.org/abs/2402.15048, LLM4EA https://arxiv.org/abs/2405.16806,
    LLM-Align https://arxiv.org/abs/2412.04690, EasyEA
    https://aclanthology.org/2025.findings-acl.1080.pdf) is: high-recall embedding candidate
    generation, then LLM reasoning ONLY as a reranker over the shortlist — never as the primary
    aligner or score source.

11. **CaRB (Bhardwaj et al., EMNLP 2019) formalizes asymmetric partial-credit tuple matching.**
    OpenIE tuples are matched slot-wise (relation with relation, argument with argument) at token
    level, with MULTI-match for recall (several system tuples may jointly cover one gold tuple) and
    single-match for precision (https://aclanthology.org/D19-1651/,
    https://github.com/dair-iitd/CaRB). Coverage/recall and precision deserve different matching
    rules — our coverage metric can be generous without inflating precision.

12. **Education prior art scores student-vs-expert graphs with explicit partial-match categories.**
    Cronus (Dahir et al., IEEE Access 2021) compares student concept maps to an instructor map and
    reports concepts/links/branches as matched, PARTIALLY matched, or missed, then grades from those
    stats (https://doi.org/10.1109/ACCESS.2021.3106509). Goldsmith's Pathfinder closeness index C
    (neighborhood-set similarity between student and instructor networks) predicted exam performance
    at r=.74 (https://link.springer.com/rwe/10.1007/978-3-319-17461-7_23). Rye & Rubba 2002 weight
    expert-map propositions by importance (https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1949-8594.2002.tb18194.x).
    Partial link credit and graded proposition matching are standard in this literature.

13. **KG-construction evaluation (2025) converged on embedding-graded triple matching.** Arxiv
    2502.05239 evaluates constructed KGs vs reference via optimal edit paths for hallucination/
    omission counts and a "Graph BERTScore" that treats edges as sentences and gives partial credit
    for synonymous entities/relations (https://arxiv.org/abs/2502.05239). Independent confirmation
    that deterministic-but-graded triple alignment, not an LLM judge, is the current default.

## Adoptable artifacts

- **smatchpp** — `pip install smatchpp`; GPL-3.0; active (v1.8.0, 2025-05). ILP-optimal alignment,
  lossless compression, custom graded triple-match functions, subgraph scoring. Adoption: verbalize
  our reference KG and parser candidates as triples; run ILP alignment with an embedding/NLI-graded
  match function; read node_coverage and edge_coverage off the SAME alignment. GPL-3.0 is the main
  caution for a proprietary backend — the algorithm is simple enough to reimplement (ILP over ~5x~15
  mapping variables via `mip` or `scipy.optimize.linear_sum_assignment` + edge terms) if license is a
  blocker. (https://github.com/flipz357/smatchpp)
- **amr-metric-suite (s2match)** — MIT; research-stage. Reference implementation of graded node
  credit with cosine threshold tau. (https://github.com/flipz357/amr-metric-suite)
- **POT (Python Optimal Transport)** — MIT, mature, maintained; `ot.gromov.fused_gromov_wasserstein`
  gives soft transport plans between attributed graphs. Adoption: node feature cost = 1 - embedding
  similarity between candidate statement and reference node label/description; structure cost from
  adjacency; read fractional coverage from the plan. (https://pythonot.github.io/)
- **FGWAlign** — code at https://github.com/squareRoot3/FGWAlign (VLDB 2025); handles edge labels
  (multi-relational). Adoption: GED-style alignment with relation-labeled edges if we outgrow the
  ILP-Smatch formulation.
- **FusedGW-Entity-Alignment (FGWEA)** — https://github.com/squareRoot3/FusedGW-Entity-Alignment
  (ACL Findings 2023). Template for the anchor-then-propagate schedule.
- **Rematch-RARE** — https://github.com/Zoher15/Rematch-RARE (NAACL Findings 2024); license not
  clearly stated. Cheap motif-overlap baseline metric.
- **CaRB scorer** — https://github.com/dair-iitd/CaRB. Slot-wise token-level partial credit +
  multi-match recall logic worth porting into our coverage computation.
- **SPICE** — https://panderson.me/spice/ (Java, old). Adopt the tuple-decomposition design, not the
  code; SoftSPICE (FACTUAL repo https://github.com/zhuang-li/FactualSceneGraph) modernizes it with
  Sentence-BERT triple-phrase similarity.

## Recall lessons

- **Make alignment joint, not pipelined.** The entire Smatch/GED/OT literature chooses node
  correspondence to maximize TOTAL agreement including edges. A node the lexical/NLI tiers cannot
  resolve is still recoverable if its incident edges match the reference topology around an anchored
  node. Our 5-node graphs make exact ILP costless; the compounding penalty of freeze-then-score is
  self-inflicted.
- **Grade, don't gate.** s2match/SoftSPICE/Graph-BERTScore all replace binary matches with bounded
  similarity in [0,1] (cosine with a floor tau). Partial credit proportional to similarity raises
  recall without the over-crediting failure of an LLM audit, because credit is capped by a measured
  similarity, not generated by a judge.
- **Score edges as verbalized triple phrases too.** SoftSPICE-style triple-phrase embedding lets a
  paraphrased relation ("more speed means less pressure") match a reference edge whose endpoints
  never individually resolved. Edge credit stops requiring two prior node resolutions.
- **Asymmetric matching for coverage.** CaRB: multi-match on the recall side (several student
  utterances jointly cover one reference node/edge), single-match on precision. Coverage is a
  reference-side recall question — do not compute it with a symmetric similarity or a precision-
  tuned threshold.
- **Candidate generation first, precision later.** The EA-with-LLM literature converged on generous
  top-k embedding shortlists per entity, with the expensive precise model only reranking. Our
  precision-first NLI threshold as a hard gate inverts this; run it as a reranker over an
  embedding-recall shortlist instead.
- **Decompose the score.** SPICE's unary-tuple trick: report node coverage and edge coverage from
  one alignment, but never let a failed relation zero out node credit that the alignment supports.
- **Anchor-and-propagate.** FGWEA's schedule (high-confidence semantic anchors, then structural
  propagation) is the small-graph-friendly version of joint alignment and matches our data: 1-2
  confident resolutions per attempt that should seed the rest.

## Dead ends

- **SemBleu / path-n-gram metrics** (https://arxiv.org/abs/1905.10726): alignment-free k-gram
  overlap compounds a single node miss into all n-grams through it — the same quadratic compounding
  we are trying to escape; also shown biased/unsymmetric in Opitz et al. 2020.
- **Raw/exact GED as the grade:** symmetric edit cost penalizes extra correct student material and
  answers "how different are these graphs," not "what fraction of the reference did the student
  teach." Use alignment-derived coverage, not edit cost. (GED's NP-completeness is irrelevant at our
  size; the semantics are the disqualifier.)
- **Supervised GNN entity alignment (GCN-Align lineage, OpenEA methods):** built for 15k-100k-entity
  KGs with seed alignment training pairs; meaningless on a 5-node reference graph. Only the
  unsupervised OT framing (FGWEA) transfers.
- **Neural GED regressors (SimGNN-style, learned GED):** need graph-pair training data; produce a
  scalar similarity, not a per-node/per-edge alignment we can turn into itemized credit.
- **LLM-as-judge as the primary aligner/score source:** our transcript-audit already over-credits;
  the 2024-2026 EA and KG-eval literature uses LLMs only to rerank embedding-generated candidate
  shortlists, with deterministic graded matching producing the score.
- **Hill-climbing Smatch:** documented search errors; with graphs our size, use the ILP solver.
- **Pathfinder closeness index alone:** requires relatedness-rating elicitation and ignores link
  labels; valuable only as historical evidence that neighborhood-structure similarity predicts exam
  performance.

## Sources

- https://amr.isi.edu/smatch-13.pdf (Smatch, Cai & Knight 2013)
- https://aclanthology.org/P13-2131/ (Smatch ACL page)
- https://github.com/snowblink14/smatch
- https://arxiv.org/abs/2305.06993 (Smatch++, Opitz 2023)
- https://github.com/flipz357/smatchpp
- https://arxiv.org/abs/2001.10929 (AMR Similarity Metrics from Principles / s2match, TACL 2020)
- https://github.com/flipz357/amr-metric-suite
- https://arxiv.org/abs/2108.11949 (Weisfeiler-Leman in the Bamboo / WWLK, TACL 2021)
- https://arxiv.org/abs/1905.10726 (SemBleu)
- https://arxiv.org/abs/2404.02126 (Rematch, NAACL Findings 2024)
- https://github.com/Zoher15/Rematch-RARE
- https://arxiv.org/abs/1607.08822 (SPICE, ECCV 2016)
- https://panderson.me/spice/
- https://arxiv.org/abs/2305.17497 (FACTUAL / SoftSPICE, ACL Findings 2023)
- https://aclanthology.org/2023.findings-acl.398.pdf
- https://github.com/zhuang-li/FactualSceneGraph
- https://www.sciencedirect.com/science/article/abs/pii/S003132031400452X (bipartite GED, Riesen & Bunke line)
- https://bougleux.users.greyc.fr/articles/ged-prl.pdf (GED as quadratic assignment)
- https://www.vldb.org/pvldb/vol18/p3641-tang.pdf (FGWAlign, VLDB 2025)
- https://github.com/squareRoot3/FGWAlign
- https://arxiv.org/abs/1805.09114 (Fused Gromov-Wasserstein, Vayer et al.)
- https://proceedings.mlr.press/v97/xu19b/xu19b.pdf (Gromov-Wasserstein Learning, ICML 2019)
- https://aclanthology.org/2023.findings-acl.205/ (FGWEA, ACL Findings 2023)
- https://github.com/squareRoot3/FusedGW-Entity-Alignment
- https://pythonot.github.io/ (POT library)
- https://aclanthology.org/D18-1032/ (GCN-Align, EMNLP 2018)
- https://github.com/nju-websoft/OpenEA (OpenEA benchmark, VLDB 2020)
- https://arxiv.org/abs/2402.15048 (ChatEA)
- https://arxiv.org/abs/2405.16806 (LLM4EA)
- https://arxiv.org/abs/2412.04690 (LLM-Align)
- https://aclanthology.org/2025.findings-acl.1080.pdf (EasyEA, ACL Findings 2025)
- https://aclanthology.org/D19-1651/ (CaRB, EMNLP 2019)
- https://github.com/dair-iitd/CaRB
- https://doi.org/10.1109/ACCESS.2021.3106509 (Cronus concept-map grader, IEEE Access 2021)
- https://link.springer.com/rwe/10.1007/978-3-319-17461-7_23 (Structural Assessment of Knowledge / Goldsmith closeness index)
- https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1949-8594.2002.tb18194.x (Rye & Rubba 2002 weighted expert-map scoring)
- https://arxiv.org/abs/2502.05239 (KG-construction eval: hallucination/omission/graph similarity, 2025)
