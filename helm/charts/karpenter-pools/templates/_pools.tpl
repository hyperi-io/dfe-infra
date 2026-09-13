{{/*
Helpers shared by the NodePool and EC2NodeClass templates.

  karpenter-pools.nodeClass  -- the pool's EC2NodeClass settings, its own
                                overrides layered over the chart defaults
  karpenter-pools.generation -- the generation number a family name carries
  karpenter-pools.validate   -- the render guards; templates/validate.yaml runs them
*/}}

{{- define "karpenter-pools.nodeClass" -}}
{{- $base := deepCopy .ctx.Values.karpenter.nodeClass -}}
{{- mergeOverwrite $base (deepCopy (.pool.nodeClass | default dict)) | toYaml -}}
{{- end -}}

{{- /* m9g -> 9, r9gd -> 9, c8gn -> 8. The first digit run after the class
       letters IS the generation, which is what makes the floor below checkable
       rather than advisory. */ -}}
{{- define "karpenter-pools.generation" -}}
{{- regexFind "[0-9]+" .family -}}
{{- end -}}

{{- define "karpenter-pools.validate" -}}
{{- $k := .ctx.Values.karpenter -}}
{{- if not $k.pools -}}
{{- /* No pools declared: this cloud does not run Karpenter, and the cluster
       facts below are not its to supply. */ -}}
{{- else -}}

{{- range $field := list "discoveryTag" "instanceProfile" "kmsKeyId" -}}
{{- if not (index $k.cluster $field) -}}
{{- fail (printf "karpenter.pools names %d pool(s) and karpenter.cluster.%s is empty -- it comes from the kubernetes-cluster module's outputs, and without it the EC2NodeClass never reaches Ready" (len $k.pools) $field) -}}
{{- end -}}
{{- end -}}

{{- $policies := list "WhenEmpty" "WhenEmptyOrUnderutilized" "Balanced" -}}
{{- range $name, $pool := $k.pools -}}

{{- if not $pool.families -}}
{{- fail (printf "karpenter.pools.%s.families is empty -- a pool with no family has nothing to launch" $name) -}}
{{- end -}}

{{- if not $pool.arch -}}
{{- fail (printf "karpenter.pools.%s.arch is empty -- cloud pools are arm64, and an unset arch lets Karpenter launch an image the DFE manifests have no layer for" $name) -}}
{{- end -}}

{{- if not $pool.capacityTypes -}}
{{- fail (printf "karpenter.pools.%s.capacityTypes is empty -- name on-demand, or spot with on-demand behind it" $name) -}}
{{- end -}}

{{- if not (has $pool.consolidation.policy $policies) -}}
{{- fail (printf "karpenter.pools.%s.consolidation.policy is %q -- the CRD takes WhenEmpty, WhenEmptyOrUnderutilized or Balanced" $name ($pool.consolidation.policy | toString)) -}}
{{- end -}}

{{- if not $pool.consolidation.after -}}
{{- fail (printf "karpenter.pools.%s.consolidation.after is empty -- the CRD requires it, and its own default of 0s consolidates the moment a node looks idle" $name) -}}
{{- end -}}

{{- if not $pool.budgetNodes -}}
{{- fail (printf "karpenter.pools.%s.budgetNodes is empty -- the CRD's default budget lets 10%% of the pool terminate at once, which takes a quorum with it on a pool of three" $name) -}}
{{- end -}}

{{- if not $pool.expireAfter -}}
{{- fail (printf "karpenter.pools.%s.expireAfter is empty -- the CRD defaults it to 720h, so leaving it out is a forced 30-day replacement rather than no expiry. Say Never when that is what is meant" $name) -}}
{{- end -}}

{{- if not $pool.limits -}}
{{- fail (printf "karpenter.pools.%s.limits is empty -- an unlimited pool answers a runaway workload by buying nodes until the account's quota stops it" $name) -}}
{{- end -}}

{{- if and (not $pool.generationGt) (not $pool.generationIn) -}}
{{- fail (printf "karpenter.pools.%s names neither generationGt nor generationIn -- without one, Karpenter may launch a family generation the shape policy rejected" $name) -}}
{{- end -}}

{{- /* Karpenter ANDs the requirements, so a floor at or above a listed family
       silently deletes that family and the fallback list it was written for. */ -}}
{{- range $family := $pool.families -}}
{{- $gen := include "karpenter-pools.generation" (dict "family" $family) -}}
{{- if not $gen -}}
{{- fail (printf "karpenter.pools.%s.families names %q, which carries no generation digit -- Karpenter's instance-family label is the full family including its generation, such as m9g" $name $family) -}}
{{- end -}}
{{- if $pool.generationIn -}}
{{- if not (has $gen ($pool.generationIn | toStrings)) -}}
{{- fail (printf "karpenter.pools.%s pins generation %v and lists family %q, which is generation %s -- the two are ANDed, so that family can never be launched" $name $pool.generationIn $family $gen) -}}
{{- end -}}
{{- else if le (int $gen) (int $pool.generationGt) -}}
{{- fail (printf "karpenter.pools.%s sets generationGt %v and lists family %q at generation %s -- the two are ANDed, so the fallback that family exists for is deleted. The floor is one BELOW the oldest family named" $name $pool.generationGt $family $gen) -}}
{{- end -}}
{{- end -}}

{{- /* The volume API takes far more than the launch template does, and the
       launch template is the path a node is created through. */ -}}
{{- $nc := include "karpenter-pools.nodeClass" (dict "ctx" $.ctx "pool" $pool) | fromYaml -}}
{{- if not $nc.rootVolume.sizeGi -}}
{{- fail (printf "karpenter.pools.%s resolves no root volume size -- set karpenter.nodeClass.rootVolume.sizeGi, or the pool's own override" $name) -}}
{{- end -}}
{{- if not $nc.rootVolume.throughputMibS -}}
{{- fail (printf "karpenter.pools.%s resolves no root volume throughput -- unset, the EBS CSI default of 125 MiB/s applies whatever the volume's size" $name) -}}
{{- end -}}
{{- if gt (int $nc.rootVolume.throughputMibS) 1000 -}}
{{- fail (printf "karpenter.pools.%s asks for %v MiB/s on the root volume; the launch template a node is created through takes 125 to 1000, whatever gp3 itself reaches through CreateVolume" $name $nc.rootVolume.throughputMibS) -}}
{{- end -}}
{{- if and $nc.rootVolume.iops (gt (int $nc.rootVolume.iops) 16000) -}}
{{- fail (printf "karpenter.pools.%s asks for %v IOPS on the root volume; the launch template takes 3000 to 16000 on gp3, whatever CreateVolume reaches" $name $nc.rootVolume.iops) -}}
{{- end -}}

{{- end -}}
{{- end -}}
{{- end -}}
