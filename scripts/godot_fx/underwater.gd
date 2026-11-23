extends Node
# Active l'effet sous-marin quand la camera est sous un plan d'eau (noeuds du groupe "water",
# crees par build_godot.py). Suit aussi la teinte de l'eau (valeurs DIVE par defaut).

@export var water_color := Color(0.080, 0.159, 0.171)
@export var absorption := 0.09
@export var caustics := true
@export var light_rays := true
@export var plankton := true

var _quad: MeshInstance3D
var _mat: ShaderMaterial

func _ready() -> void:
	_mat = ShaderMaterial.new()
	_mat.shader = load("res://%s/shaders/underwater.gdshader" % _root_dir())
	var noise := load("res://%s/fx/perlin2d.png" % _root_dir())
	if noise:
		_mat.set_shader_parameter("noise_tex", noise)
	# quad plein ecran (le vertex shader le colle a l'ecran) ; fixe a la camera active
	_quad = MeshInstance3D.new()
	var m := QuadMesh.new()
	m.size = Vector2(2, 2)
	m.flip_faces = true
	_quad.mesh = m
	_quad.material_override = _mat
	_quad.extra_cull_margin = 16384.0
	_quad.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	_quad.visible = false
	add_child(_quad)

func _root_dir() -> String:
	# dossier du projet genere, deduit du chemin de ce script (res://<dossier>/scripts/underwater.gd)
	return get_script().resource_path.get_base_dir().get_base_dir().trim_prefix("res://")

func _water_level_at(p: Vector3) -> float:
	# niveau de l'eau au-dessus de p (NAN si aucun plan d'eau ne couvre ce point)
	var best := NAN
	for n in get_tree().get_nodes_in_group("water"):
		if not (n is MeshInstance3D) or n.mesh == null:
			continue
		var half: Vector2 = (n.mesh as PlaneMesh).size * 0.5
		var c: Vector3 = n.global_position
		if absf(p.x - c.x) <= half.x and absf(p.z - c.z) <= half.y and p.y < c.y:
			if is_nan(best) or c.y < best:
				best = c.y
	return best

func _process(_delta: float) -> void:
	var cam := get_viewport().get_camera_3d()
	if cam == null:
		_quad.visible = false
		return
	var level := _water_level_at(cam.global_position)
	var under := not is_nan(level)
	_quad.visible = under
	if not under:
		return
	if _quad.get_parent() != cam:
		_quad.reparent(cam, false)
		_quad.transform = Transform3D.IDENTITY
	_mat.set_shader_parameter("surface_y", level)
	_mat.set_shader_parameter("water_color", water_color)
	_mat.set_shader_parameter("absorption", absorption)
	_mat.set_shader_parameter("caustics_enabled", caustics)
	_mat.set_shader_parameter("rays_enabled", light_rays)
	_mat.set_shader_parameter("plankton_enabled", plankton)
