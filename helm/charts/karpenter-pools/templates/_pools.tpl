{{/*
Helpers shared by the NodePool and EC2NodeClass templates.

  karpenter-pools.nodeClass  -- the pool's EC2NodeClass settings, its own
                                overrides layered over the chart defaults
  karpenter-pools.generation -- the generation number a family name carries
  karpenter-pools.terminationGracePeriod -- the pool's drain bound, its own over
                                the chart's
  karpenter-pools.validate   -- the render guards; templates/validate.yaml runs them
*/}}

{{- define "karpenter-pools.nodeClass" -}}
{{- $base := deepCopy .ctx.Values.karpenter.nodeClass -}}
{{- mergeOverwrite $base (deepCopy (.pool.nodeClass | default dict)) | toYaml -}}
{{- end -}}

{{- define "karpenter-pools.terminationGracePeriod" -}}
{{- .pool.terminationGracePeriod | default .ctx.Values.karpenter.terminationGracePeriod | default "" | toString -}}
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

{{- /* sprig's b64dec renders its own error text in place of a script it cannot decode. */ -}}
{{- with $k.cluster.userData -}}
{{- if not (regexMatch "^[A-Za-z0-9+/]+={0,2}$" .) -}}
{{- fail "karpenter.cluster.userData is not base64 -- it is the cloud root's DFE_NODE_USER_DATA output, carried unchanged" -}}
{{- end -}}
{{- end -}}

{{- $policies := list "WhenEmpty" "WhenEmptyOrUnderutilized" "Balanced" -}}
{{- $reasons := list "Underutilized" "Empty" "Drifted" -}}
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

{{- if not $pool.budgets -}}
{{- fail (printf "karpenter.pools.%s.budgets is empty -- the CRD's default budget lets 10%% of the pool terminate at once, which takes a quorum with it on a pool of three" $name) -}}
{{- end -}}

{{- /* The CRD's own patterns for nodes and duration, and its pairing rule for schedule. */ -}}
{{- range $i, $budget := $pool.budgets -}}
{{- if not (regexMatch "^((100|[0-9]{1,2})%|[0-9]+)$" (toString $budget.nodes)) -}}
{{- fail (printf "karpenter.pools.%s.budgets[%d].nodes is %q -- the CRD takes a node count or a whole percent up to 100%%, such as 1 or 25%%" $name $i (toString $budget.nodes)) -}}
{{- end -}}
{{- range $reason := ($budget.reasons | default list) -}}
{{- if not (has $reason $reasons) -}}
{{- fail (printf "karpenter.pools.%s.budgets[%d] names reason %q -- the CRD takes Underutilized, Empty or Drifted, and no reasons at all means every one" $name $i (toString $reason)) -}}
{{- end -}}
{{- end -}}
{{- if and $budget.schedule (not $budget.duration) -}}
{{- fail (printf "karpenter.pools.%s.budgets[%d] sets schedule with no duration -- the CRD refuses either without the other" $name $i) -}}
{{- end -}}
{{- if and $budget.duration (not $budget.schedule) -}}
{{- fail (printf "karpenter.pools.%s.budgets[%d] sets duration with no schedule -- the CRD refuses either without the other" $name $i) -}}
{{- end -}}
{{- with $budget.duration -}}
{{- if not (regexMatch "^((([0-9]+(h|m))|([0-9]+h[0-9]+m))(0s)?)$" (toString .)) -}}
{{- fail (printf "karpenter.pools.%s.budgets[%d].duration is %q -- the CRD takes hours and minutes only, such as 20h or 3h30m" $name $i (toString .)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* An inactive budget allows everything, so a reason only scheduled budgets
       name is unlimited whenever none of them is running. */ -}}
{{- range $reason := $reasons -}}
{{- $covered := false -}}
{{- range $budget := $pool.budgets -}}
{{- if and (not $budget.schedule) (or (not $budget.reasons) (has $reason ($budget.reasons | default list))) -}}
{{- $covered = true -}}
{{- end -}}
{{- end -}}
{{- if not $covered -}}
{{- fail (printf "karpenter.pools.%s names no always-on budget for %s -- Karpenter leaves a reason no active budget names unlimited, so add one with no schedule" $name $reason) -}}
{{- end -}}
{{- end -}}

{{- if not $pool.expireAfter -}}
{{- fail (printf "karpenter.pools.%s.expireAfter is empty -- the CRD defaults it to 720h, so leaving it out is a forced 30-day replacement rather than no expiry. Say Never when that is what is meant" $name) -}}
{{- end -}}

{{- /* Karpenter's own CRD pattern. With no value a blocked drain waits forever. */ -}}
{{- $grace := include "karpenter-pools.terminationGracePeriod" (dict "ctx" $.ctx "pool" $pool) -}}
{{- if not (regexMatch "^([0-9]+(s|m|h))+$" $grace) -}}
{{- fail (printf "karpenter.pools.%s resolves terminationGracePeriod %q -- Karpenter takes digits then s, m or h, such as 24h, and with none a drain a PodDisruptionBudget blocks waits forever. Set karpenter.terminationGracePeriod, or the pool's own" $name $grace) -}}
{{- end -}}

{{- if not $pool.limits -}}
{{- fail (printf "karpenter.pools.%s.limits is empty -- an unlimited pool answers a runaway workload by buying nodes until the account's quota stops it" $name) -}}
{{- end -}}

{{- if and (not $pool.generationGt) (not $pool.generationIn) -}}
{{- fail (printf "karpenter.pools.%s names neither generationGt nor generationIn -- without one, Karpenter may launch a family generation the shape policy rejected" $name) -}}
{{- end -}}

{{- /* Karpenter ANDs the requirements, so a floor at or above a listed family
       silently deletes that family. */ -}}
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
{{- fail (printf "karpenter.pools.%s sets generationGt %v and lists family %q at generation %s -- the two are ANDed, so that family can never be launched. The floor is one BELOW the oldest family named" $name $pool.generationGt $family $gen) -}}
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
