extends Node
# Cycle jour/nuit : place le soleil et les deux lunes, pilote le ciel (sky.gdshader), la lumiere,
# l'ambiance et le brouillard atmospherique.
# Reglages (heure) : menu pause > Level, reserve a l'hote, synchronises en LAN
# A l'exterieur seulement (world.tscn). Reglages dans l'inspecteur.

@export var real_time := true                 # suit l'heure locale de la machine (IRL)
@export_range(0.0, 1.0, 0.01) var cloud_density := 0.43   # 0 ciel degage .. 1 couvert (menu pause > Level)
@export var time_of_day := 10.0               # heures (0-24)
@export var day_length := 0.0                 # secondes reelles pour 24 h ; 0 = temps fige
@export var sun_energy := 1.5
@export var moon_energy := 0.14
@export var secunda_orbit := 0.0873           # rad (5 deg) : distance angulaire Secunda <-> Masser
@export_range(0.0, 5.0, 0.05) var fog_thickness := 1.0   # x densite de la brume (0 = aucune)
@export var view_distance := 0.0              # m : distance ou tout disparait dans la brume (0 = brume legere par defaut)
@export var sun: DirectionalLight3D
@export var world_environment: WorldEnvironment
@export_range(0.0, 1.0) var overcast := 0.0   # 0 clair .. 1 couvert (pilote par weather.gd)

var _sky_mat: ShaderMaterial
var _env: Environment

func _ready() -> void:
	if sun == null:
		sun = get_parent().get_node_or_null("Sun") as DirectionalLight3D
	if world_environment == null:
		world_environment = get_parent().get_node_or_null("WorldEnvironment") as WorldEnvironment
	if world_environment:
		_env = world_environment.environment
		if _env and _env.sky:
			_sky_mat = _env.sky.sky_material as ShaderMaterial
	if real_time:
		time_of_day = _clock_hours()
	_update()

func _clock_hours() -> float:
	var d := Time.get_datetime_dict_from_system()   # heure locale
	return float(d.hour) + float(d.minute) / 60.0 + float(d.second) / 3600.0

func _process(delta: float) -> void:
	if real_time:
		time_of_day = _clock_hours()
	elif day_length > 0.0:
		time_of_day = fposmod(time_of_day + 24.0 * delta / day_length, 24.0)
	_update()

# Epaisseur de la brume : AUTOMATIQUE en heure reelle (dense a l'aube (pic vers 6h30), elle se
# leve dans la matinee, legere l'apres-midi, voile leger le soir) ; en heure fixe, c'est le reglage fog_thickness.
func _fog_time_factor() -> float:
	var morning := exp(-pow((time_of_day - 6.5) / 2.0, 2.0))
	var evening := exp(-pow((time_of_day - 19.5) / 1.8, 2.0))
	return 0.55 + 2.4 * morning + 0.6 * evening

func _fog_mul() -> float:
	return _fog_time_factor() if real_time else fog_thickness

func _dir(angle: float, tilt: float) -> Vector3:
	return Vector3(cos(angle), sin(angle), tilt).normalized()

func _update() -> void:
	var a := (time_of_day - 6.0) / 12.0 * PI        # lever a 6 h, coucher a 18 h
	var sun_dir := _dir(a, 0.28)
	var masser_dir := _dir(a + 2.35, -0.35)
	# Secunda orbite autour de Masser (ecart angulaire fixe)
	var mt := masser_dir.cross(Vector3.UP).normalized()
	var mb := masser_dir.cross(mt)
	var phi := a * 1.7 + 0.8
	var secunda_dir := (masser_dir * cos(secunda_orbit) + (mt * cos(phi) + mb * sin(phi)) * sin(secunda_orbit)).normalized()
	var sun_h := sun_dir.y
	var day := smoothstep(-0.12, 0.22, sun_h)
	var night := 1.0 - smoothstep(-0.25, 0.05, sun_h)
	var twilight := clampf(1.0 - absf(sun_h) * 4.0, 0.0, 1.0)

	if sun:
		var e_sun := sun_energy * smoothstep(-0.04, 0.2, sun_h) * (1.0 - overcast * 0.75)
		var e_moon := moon_energy * night * maxf(masser_dir.y, 0.0)
		var warm := Color(1.0, 0.55, 0.3).lerp(Color(1.0, 0.96, 0.88), clampf(sun_h * 3.0, 0.0, 1.0))
		if e_sun >= e_moon:
			sun.light_energy = e_sun
			sun.light_color = warm
			sun.global_transform = Transform3D(Basis.looking_at(-sun_dir, Vector3.UP), sun.global_position)
		else:
			sun.light_energy = e_moon
			sun.light_color = Color(0.55, 0.65, 1.0)
			sun.global_transform = Transform3D(Basis.looking_at(-masser_dir, Vector3.UP), sun.global_position)
		sun.shadow_enabled = sun.light_energy > 0.02

	if _sky_mat:
		_sky_mat.set_shader_parameter("sun_dir", sun_dir)
		_sky_mat.set_shader_parameter("masser_dir", masser_dir)
		_sky_mat.set_shader_parameter("secunda_dir", secunda_dir)
		_sky_mat.set_shader_parameter("overcast", overcast)
		_sky_mat.set_shader_parameter("cloud_density", cloud_density)

	if _env:
		var horizon := Color(0.015, 0.025, 0.05).lerp(Color(0.58, 0.72, 0.88), day)
		horizon = horizon.lerp(Color(0.86, 0.93, 1.0), 0.6 * day)          # meme clarte que l'horizon du ciel (pas de silhouette)
		horizon = horizon.lerp(Color(0.95, 0.55, 0.35), twilight * 0.35)
		horizon = horizon.lerp(Color(0.5, 0.52, 0.56) * day + Color(0.02, 0.025, 0.04) * (1.0 - day), overcast * 0.7)
		_env.fog_light_color = horizon
		var clear_density := 0.00035 if view_distance <= 0.0 else 3.9 / view_distance   # exp(-d*D) ~ 2 % a view_distance
		_env.fog_density = lerpf(clear_density, clear_density * 2.2, overcast) * _fog_mul()
		_env.ambient_light_energy = lerpf(0.35, 1.0, day)
		if _env.volumetric_fog_enabled:
			_env.volumetric_fog_albedo = Color(1.0, 0.85, 0.65).lerp(Color(0.8, 0.85, 1.0), night)
			_env.volumetric_fog_density = lerpf(0.0006, 0.004, overcast) * _fog_mul()
