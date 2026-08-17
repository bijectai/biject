-- Root module for the sprint-v4 PolicyEnv library (biject demo repo).
-- Imports all sub-modules so `lake build` compiles everything.
--
-- S4-D-13: audit-bound predicate for the EDC write-correction demo.
-- This is a standalone Lake project; it mirrors the layout of the platform's
-- lean-worker/PolicyEnv package (bijectai/biject-api) so the modules can be
-- registered there unchanged when the demo predicate is promoted.
import PolicyEnv.Contract
import PolicyEnv.AuditBound
