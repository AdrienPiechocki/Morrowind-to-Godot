extends Node3D
# Mont Ecarlate : panache de fumee volumetrique + impostor de la montagne (visible de partout),
# places a leur position reelle sur la carte de Morrowind (unites Morrowind -> metres Godot).
# Le noeud doit rester a l'origine du monde (comme farlands.tscn) : les positions sont absolues.

const MW_TO_M := 0.01

@export var crater_mw := Vector3(20848.0, 69177.0, 12345.0)         # cratere (x est, y nord, z haut)
@export var photo_origin_mw := Vector3(20848.0, 68977.0, 11220.0)   # reference des vues de l'impostor
@export var altitude_offset_m := -12.0   # abaisse tout le volcan (montagne + fumee) : le sol du cratere est a ~101-112 m dans les donnees, le pack le place a 123 m
@export var show_plume := true
@export var show_impostor := true     # inutile si le vrai terrain du volcan est dans la scene (farlands)
@export var plume_height_m := 860.0
@export var plume_radius_top_m := 211.0
@export var plume_bend_m := 650.0
@export var base_sink_m := 30.0      # la colonne nait sous le bord du cratere (cachee par l'arete avant)
@export var wind_mw := Vector2(-0.8, -0.35)                         # sens du vent (x est, y nord)
@export_range(8, 128) var plume_steps := 56
@export var haze_density := 0.00025  # brume propre au volcan (1/m), plus legere que la brume du monde : visible de loin
@export var fade_start_m := 1500.0   # le volcan devient transparent a partir de cette distance...
@export var fade_end_m := 7000.0     # ...et disparait ici
@export var daynight: Node

var _plume_mat: ShaderMaterial
var _mountain_mat: ShaderMaterial
var _sun: DirectionalLight3D
var _env: Environment
var _imp_center := Vector3.ZERO
var _imp_size := 896.87

static func mw_to_godot(p: Vector3) -> Vector3:
	# Morrowind (x est, y nord, z haut) -> Godot (x est, y haut, z = -nord), en metres
	return Vector3(p.x, p.z, -p.y) * MW_TO_M

func _root_dir() -> String:
	return get_script().resource_path.get_base_dir().get_base_dir().trim_prefix("res://")

func _ready() -> void:
	if daynight == null:
		daynight = get_parent().get_node_or_null("DayNight")
	if daynight:
		_sun = daynight.get("sun")
		var we: WorldEnvironment = daynight.get("world_environment")
		_env = we.environment if we else null
	if show_plume:
		_build_plume()
	if show_impostor:
		_build_impostor()

func _build_plume() -> void:
	var origin := mw_to_godot(crater_mw) + Vector3(0.0, altitude_offset_m, 0.0)
	var wind := Vector2(wind_mw.x, -wind_mw.y).normalized()          # nord MW = -z Godot
	var reach := plume_radius_top_m + plume_bend_m
	var bmin := Vector3(origin.x - reach, origin.y - base_sink_m - 2.0, origin.z - reach)
	var bmax := Vector3(origin.x + reach, origin.y + plume_height_m, origin.z + reach)
	var mesh := BoxMesh.new()
	mesh.size = bmax - bmin
	var mi := MeshInstance3D.new()
	mi.name = "Plume"
	mi.mesh = mesh
	mi.position = (bmin + bmax) * 0.5
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	mi.extra_cull_margin = 4096.0
	_plume_mat = ShaderMaterial.new()
	_plume_mat.shader = load("res://%s/shaders/redmountain_plume.gdshader" % _root_dir())
	_plume_mat.set_shader_parameter("box_min", bmin)
	_plume_mat.set_shader_parameter("box_max", bmax)
	_plume_mat.set_shader_parameter("origin", origin)
	_plume_mat.set_shader_parameter("height", plume_height_m)
	_plume_mat.set_shader_parameter("base_sink", base_sink_m)
	_plume_mat.set_shader_parameter("radius_top", plume_radius_top_m)
	_plume_mat.set_shader_parameter("bend", plume_bend_m)
	_plume_mat.set_shader_parameter("wind_dir", wind)
	_plume_mat.set_shader_parameter("steps", plume_steps)
	var bn := load("res://%s/fx/redmountain_bluenoise.png" % _root_dir())
	if bn:
		_plume_mat.set_shader_parameter("blue_noise", bn)
	_plume_mat.render_priority = 0           # AVANT l'impostor : la montagne passe devant la fumee
	mi.material_override = _plume_mat
	add_child(mi)

func _build_impostor() -> void:
	var dir := "res://%s/fx/red-mountain/" % _root_dir()
	var names := ["0-15", "16-31", "32-47", "48-63"]
	_mountain_mat = ShaderMaterial.new()
	_mountain_mat.shader = load("res://%s/shaders/redmountain_mountain.gdshader" % _root_dir())
	for i in 4:
		var t := load(dir + names[i] + ".png")
		if t == null:
			push_warning("Red Mountain: %s%s.png introuvable, impostor ignore" % [dir, names[i]])
			return
		_mountain_mat.set_shader_parameter("atlas%d" % i, t)
	# plan vertical centre sur l'origine des photos, abaisse de la hauteur d'appareil (z/2)
	var ref := mw_to_godot(photo_origin_mw) + Vector3(0.0, altitude_offset_m * 2.0, 0.0)   # le plan est centre a ref.y / 2
	var photo_dist := 81920.0 * MW_TO_M                      # 10 cells
	var half_tan := 0.57735027 * (512.0 / 540.0)
	var size := photo_dist * half_tan * 2.0
	_mountain_mat.set_shader_parameter("size", size)
	_imp_size = size
	var quad := QuadMesh.new()
	quad.size = Vector2(1, 1)
	var mi := MeshInstance3D.new()
	mi.name = "MountainImpostor"
	mi.mesh = quad
	mi.position = Vector3(ref.x, ref.y * 0.5, ref.z)
	_imp_center = mi.position
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	mi.extra_cull_margin = 4096.0
	_mountain_mat.render_priority = 10       # apres le panache : elle le recouvre
	mi.material_override = _mountain_mat
	add_child(mi)

func _process(_delta: float) -> void:
	var sun_dir := Vector3(0.3, 0.8, 0.5).normalized()
	var factor := 1.0
	var sun_col := Color(1.0, 0.95, 0.85)
	var fog_col := Color(0.58, 0.72, 0.88)
	var haze := haze_density
	if _sun:
		sun_dir = _sun.global_transform.basis.z.normalized()
		factor = clampf(_sun.light_energy / 1.5, 0.0, 1.0)
		sun_col = _sun.light_color
	if _env:
		fog_col = _env.fog_light_color
	var light := clampf(factor + 0.12, 0.0, 1.0)
	if _plume_mat:
		_plume_mat.set_shader_parameter("fade_start", fade_start_m)
		_plume_mat.set_shader_parameter("fade_end", fade_end_m)
		_plume_mat.set_shader_parameter("sun_dir", sun_dir)
		_plume_mat.set_shader_parameter("sun_color", Vector3(sun_col.r, sun_col.g, sun_col.b) * factor)
		_plume_mat.set_shader_parameter("ambient_color", Vector3(fog_col.r, fog_col.g, fog_col.b) * (0.25 + 0.75 * factor))
		_plume_mat.set_shader_parameter("fog_color", Vector3(fog_col.r, fog_col.g, fog_col.b))
		_plume_mat.set_shader_parameter("haze_density", haze)
	if _mountain_mat:
		_mountain_mat.set_shader_parameter("fade_start", fade_start_m)
		_mountain_mat.set_shader_parameter("fade_end", fade_end_m)
		_mountain_mat.set_shader_parameter("light", light)
		_mountain_mat.set_shader_parameter("fog_color", Vector3(fog_col.r, fog_col.g, fog_col.b) * 0.95)
		_mountain_mat.set_shader_parameter("haze_density", maxf(haze, _env.fog_density if _env else 0.0))
