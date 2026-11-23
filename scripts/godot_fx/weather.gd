extends Node3D
# Meteo : pluie (particules autour de la camera, ciel couvert, ondulations sur l'eau).
#   R : pluie on/off.   Equivalent simplifie de la partie "pluie" de wetworld (Rafael's Shader Pack).

@export var raining := false
@export var rain_amount := 6000
@export var daynight: Node            # daynight.gd (ciel couvert) ; trouve automatiquement sinon

# direction de chute (legere inclinaison due au vent) ; les stries sont orientees selon cette direction 3D
const DROP_DIR := Vector3(0.12, -1.0, 0.05)
const RAIN_SHADER := """shader_type spatial;
render_mode unshaded, blend_mix, depth_draw_never, cull_disabled, shadows_disabled, fog_disabled;
uniform vec3 drop_dir = vec3(0.12, -1.0, 0.05);
uniform float streak_length = 0.5;
uniform float streak_width = 0.012;
uniform vec4 color : source_color = vec4(0.75, 0.82, 0.95, 0.4);
varying float cam_dist;
void vertex() {
	// strie = segment oriente selon la trajectoire reelle de la goutte, face a la camera autour de cet axe :
	// en regardant vers le haut ou le bas, elle se raccourcit en perspective comme une vraie goutte
	vec3 center = MODEL_MATRIX[3].xyz;
	vec3 cam = (INV_VIEW_MATRIX * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
	vec3 axis = normalize(drop_dir);
	vec3 to_cam = cam - center;
	cam_dist = length(to_cam);
	vec3 side = cross(axis, normalize(to_cam));
	float sl = length(side);
	side = sl > 1e-4 ? side / sl : normalize(cross(axis, vec3(1.0, 0.0, 0.0)));
	vec3 world = center + side * VERTEX.x * streak_width + axis * VERTEX.y * streak_length;
	POSITION = PROJECTION_MATRIX * (VIEW_MATRIX * vec4(world, 1.0));
}
void fragment() {
	float edge = 1.0 - abs(UV.x * 2.0 - 1.0);                  // bords adoucis
	float ends = smoothstep(0.0, 0.25, UV.y) * smoothstep(1.0, 0.75, UV.y);
	ALBEDO = color.rgb;
	ALPHA = color.a * edge * ends * smoothstep(2.0, 6.0, cam_dist);   // pas de gouttes geantes contre la camera
}
"""

var _rain: GPUParticles3D
var _intensity := 0.0                 # 0..1, lisse
var _water_mats: Array[ShaderMaterial] = []

func _ready() -> void:
	if daynight == null:
		daynight = get_parent().get_node_or_null("DayNight")
	_build_rain()
	_collect_water_materials()

func _collect_water_materials() -> void:
	for n in get_tree().get_nodes_in_group("water"):
		if n is MeshInstance3D and n.mesh and n.mesh.material is ShaderMaterial:
			if not _water_mats.has(n.mesh.material):
				_water_mats.append(n.mesh.material)

func _build_rain() -> void:
	_rain = GPUParticles3D.new()
	_rain.amount = rain_amount
	_rain.lifetime = 1.1
	_rain.local_coords = false
	_rain.emitting = false
	_rain.visibility_aabb = AABB(Vector3(-40, -30, -40), Vector3(80, 60, 80))
	var pm := ParticleProcessMaterial.new()
	pm.emission_shape = ParticleProcessMaterial.EMISSION_SHAPE_BOX
	pm.emission_box_extents = Vector3(30, 0.5, 30)
	pm.direction = DROP_DIR
	pm.spread = 2.0
	pm.initial_velocity_min = 26.0
	pm.initial_velocity_max = 32.0
	pm.gravity = Vector3(0, -10, 0)
	_rain.process_material = pm
	var quad := QuadMesh.new()
	quad.size = Vector2(1, 1)
	var sh := Shader.new()
	sh.code = RAIN_SHADER
	var m := ShaderMaterial.new()
	m.shader = sh
	m.set_shader_parameter("drop_dir", DROP_DIR)
	quad.material = m
	_rain.draw_pass_1 = quad
	_rain.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(_rain)

func _unhandled_key_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and event.keycode == KEY_R:
		raining = not raining

func _process(delta: float) -> void:
	_intensity = move_toward(_intensity, 1.0 if raining else 0.0, delta * 0.25)
	var cam := get_viewport().get_camera_3d()
	if cam:
		_rain.global_position = cam.global_position + Vector3(0, 18, 0)
	_rain.emitting = _intensity > 0.02
	_rain.amount_ratio = clampf(_intensity, 0.05, 1.0)
	if daynight and "overcast" in daynight:
		daynight.overcast = _intensity
	for m in _water_mats:
		m.set_shader_parameter("rain_intensity", _intensity)
