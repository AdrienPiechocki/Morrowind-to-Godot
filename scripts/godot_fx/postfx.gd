extends Node
# Post-traitement : anti-crenelage SMAA (+ TAA optionnel), regle sur le viewport.
# Le reste (SSAO = HBAO, bloom, tonemap filmique, brouillard volumetrique = godrays) est dans l'Environment
# de la scene (WorldEnvironment) et reste modifiable dans l'inspecteur.
# Equivalents Godot de SMAA / SMB (filtrage temporel) / HBAO / tonemap / godrays de Rafael's Shader Pack.

@export var smaa := true
@export var temporal_aa := false      # TAA : filtrage temporel (peut laisser des trainees)

func _ready() -> void:
	var vp := get_viewport()
	vp.screen_space_aa = Viewport.SCREEN_SPACE_AA_SMAA if smaa else Viewport.SCREEN_SPACE_AA_DISABLED
	vp.use_taa = temporal_aa
