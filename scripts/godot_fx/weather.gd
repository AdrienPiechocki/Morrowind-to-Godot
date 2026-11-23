extends Node3D
# Meteo : pluie (particules autour de la camera, ciel couvert, ondulations sur l'eau).
#   R : pluie on/off.   Equivalent simplifie de la partie "pluie" de wetworld (Rafael's Shader Pack).

@export var raining := false
@export var rain_amount := 6000
@export var daynight: Node            # daynight.gd (ciel couvert) ; trouve automatiquement sinon

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
	pm.direction = Vector3(0.12, -1, 0.05)
	pm.spread = 2.0
	pm.initial_velocity_min = 26.0
	pm.initial_velocity_max = 32.0
	pm.gravity = Vector3(0, -10, 0)
	_rain.process_material = pm
	var quad := QuadMesh.new()
	quad.size = Vector2(0.01, 0.45)
	var m := StandardMaterial3D.new()
	m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	m.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	m.albedo_color = Color(0.75, 0.82, 0.95, 0.35)
	m.billboard_mode = BaseMaterial3D.BILLBOARD_PARTICLES
	m.billboard_keep_scale = true
	m.distance_fade_mode = BaseMaterial3D.DISTANCE_FADE_PIXEL_ALPHA     # pas de gouttes geantes devant la camera
	m.distance_fade_min_distance = 2.5
	m.distance_fade_max_distance = 7.0
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
