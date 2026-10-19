{{/*
The renderer-neutral half of the pool chart: the shape every renderer reads, and
the renderer the classes go to.

  pools.validate -- the shape guards, then the selected renderer's own. Every
                    template that renders an object runs it first, because Helm
                    renders a deeper template path before templates/validate.yaml.
*/}}

{{- define "pools.validate" -}}
{{- $p := .ctx.Values.pools -}}

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
{{- end -}}

{{- if eq $p.renderer "karpenter-aws" -}}
{{- include "karpenter-aws.validate" . -}}
{{- end -}}
{{- end -}}
