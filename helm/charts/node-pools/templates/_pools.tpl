{{/*
The renderer-neutral half of the pool chart: the shape every renderer reads, and
the renderer the classes go to.

  pools.validate -- the shape guards, then the selected renderer's own. Every
                    template that renders an object runs it first, because Helm
                    renders a deeper template path before templates/validate.yaml.
  pools.keys     -- fails on a key of .value outside .allowed, naming where it
                    sits (.at) and the keys that level takes.
*/}}

{{- define "pools.keys" -}}
{{- if not (kindIs "map" .value) -}}
{{- fail (printf "%s is not a map -- it takes %s" .at (join ", " .allowed)) -}}
{{- end -}}
{{- range $key, $_ := .value -}}
{{- if not (has $key $.allowed) -}}
{{- fail (printf "%s.%s is not %s -- %s takes %s" $.at $key $.what $.at (join ", " $.allowed)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "pools.validate" -}}
{{- $p := .ctx.Values.pools -}}

{{- /* Helm ignores a values key no template reads, so an overlay written for a karpenter root would deploy as if it were absent. */ -}}
{{- if hasKey .ctx.Values "karpenter" -}}
{{- fail "karpenter is not a values root of this chart -- each pool goes under pools.classes, terminationGracePeriod under pools, and the cluster facts, tags, runInstanceSizes and nodeClass under pools.aws" -}}
{{- end -}}

{{- /* Cluster facts with no classes is a lost pool annotation, and the empty render it makes would prune every live NodePool, renderer or not. */ -}}
{{- $cluster := ($p.aws | default dict).cluster | default dict -}}
{{- if and (not $p.classes) $cluster.discoveryTag -}}
{{- fail (printf "pools.aws.cluster.discoveryTag is %q, so this cluster runs Karpenter, and pools.classes is empty -- rendering no NodePool would delete every live one. The classes reach this chart on the cluster secret's dfe.hyperi.io/node_pools annotation, which bootstrap.sh writes from sizing/<tier>.pools.json" (toString $cluster.discoveryTag)) -}}
{{- end -}}

{{- $roots := list "renderer" "terminationGracePeriod" "classes" "aws" -}}
{{- range $key, $_ := $p -}}
{{- if not (has $key $roots) -}}
{{- fail (printf "pools.%s is not a pools setting -- the root holds renderer, terminationGracePeriod, classes and aws, and a pool goes under pools.classes" $key) -}}
{{- end -}}
{{- end -}}

{{- $renderers := list "karpenter-aws" -}}
{{- if and $p.renderer (not (has $p.renderer $renderers)) -}}
{{- fail (printf "pools.renderer is %q -- this chart renders karpenter-aws only" (toString $p.renderer)) -}}
{{- end -}}
{{- if and $p.classes (not $p.renderer) -}}
{{- fail (printf "pools.classes names %d class(es) and pools.renderer is empty -- the resolver writes the renderer beside the classes, karpenter-aws on AWS" (len $p.classes)) -}}
{{- end -}}

{{- /* A renderer's own fields sit under its key, so no class carries them beside the shared ones. */ -}}
{{- $shared := list "arch" "capacityTypes" "consolidation" "budgets" "expireAfter" "terminationGracePeriod" "minVcpu" "minMemoryGib" "taints" "labels" "limits" "weight" -}}
{{- $awsOnly := list "families" "generationGt" "generationIn" "nodeClass" -}}
{{- range $name, $class := $p.classes -}}
{{- if not (kindIs "map" $class) -}}
{{- fail (printf "pools.classes.%s is not a map of pool class fields" $name) -}}
{{- end -}}
{{- range $key, $_ := $class -}}
{{- if has $key $awsOnly -}}
{{- fail (printf "pools.classes.%s.%s is an AWS field at the class's top level -- it goes under pools.classes.%s.aws" $name $key $name) -}}
{{- else if not (or (has $key $shared) (eq $key "aws")) -}}
{{- fail (printf "pools.classes.%s.%s is not a pool class field -- values.yaml lists the fields a class takes" $name $key) -}}
{{- end -}}
{{- end -}}

{{- /* These render through toYaml or by name, so a misspelt key inside one reaches the cluster or vanishes without an error. */ -}}
{{- with $class.limits -}}
{{- include "pools.keys" (dict "at" (printf "pools.classes.%s.limits" $name) "value" . "allowed" (list "cpu" "memory") "what" "a pool limit") -}}
{{- end -}}
{{- with $class.consolidation -}}
{{- include "pools.keys" (dict "at" (printf "pools.classes.%s.consolidation" $name) "value" . "allowed" (list "policy" "after") "what" "a consolidation field") -}}
{{- end -}}
{{- range $field := list "budgets" "taints" -}}
{{- $items := index $class $field -}}
{{- if and $items (not (kindIs "slice" $items)) -}}
{{- fail (printf "pools.classes.%s.%s is not a list" $name $field) -}}
{{- end -}}
{{- end -}}
{{- range $i, $budget := ($class.budgets | default list) -}}
{{- include "pools.keys" (dict "at" (printf "pools.classes.%s.budgets[%d]" $name $i) "value" $budget "allowed" (list "nodes" "reasons" "schedule" "duration") "what" "a budget field") -}}
{{- end -}}
{{- range $i, $taint := ($class.taints | default list) -}}
{{- include "pools.keys" (dict "at" (printf "pools.classes.%s.taints[%d]" $name $i) "value" $taint "allowed" (list "key" "value" "effect") "what" "a taint field") -}}
{{- end -}}
{{- end -}}

{{- if eq $p.renderer "karpenter-aws" -}}
{{- include "karpenter-aws.validate" . -}}
{{- end -}}
{{- end -}}
