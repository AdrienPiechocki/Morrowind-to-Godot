extends Node
## Décor vivant du niveau Morrowind, ajouté À L'EXÉCUTION (rien n'est écrit dans les .gltf) :
##   - lanternes / lampes : lumière chaude + halo émissif (extérieur : seulement quand le soleil est bas) ;
##   - lanternes suspendues : léger balancement (pendule autour du point d'accroche) ;
##   - petits objets sans collision (bouteilles, bols, livres…) : RigidBody3D que le joueur peut pousser.
## Les objets physiques se prennent / lancent / cassent (prop_body.gd).
## Les nœuds créés n'ont pas d'owner et leur parent porte « lan_skip_children » : le LevelBaker les ignore,
## chaque machine les reconstruit au chargement (comme les portes). CYBERREALM_NO_PROPS=1 désactive tout.

const PropBodyScript := preload("res://user/Levels/Morrowind/scripts/prop_body.gd")

const GLOW_WORDS := ["lantern", "lightpost", "lamp", "candle", "torch", "sconce", "chandelier"]
const NO_HALO_WORDS := ["lightpost", "candle", "torch"]            # lumière seule, pas de halo sur le mesh
const SWAY_WORDS := ["lantern"]                                     # suspendues
const PHYS_WORDS := ["bottle", "cup", "bowl", "plate", "potion", "mug", "goblet", "tankard", "pitcher", "jug",
	"flask", "vial", "bk_", "book", "scroll", "sc_paper", "ingred", "apparatus", "stoneware", "redware", "pot_",
	"skull", "spoon", "fork", "knife", "mortar"]
const NOT_PHYS_WORDS := ["-colonly", "candle", "lantern", "plant", "light", "bed", "rack", "shelf", "table", "post"]
const PHYS_MIN_SIZE := 0.04               # m : plus petit = poussière
const PHYS_MAX_SIZE := 0.5                # m : plus grand = meuble
const WAKE_RADIUS := 3.5                  # m : les objets se « réveillent » à cette distance du joueur
const SWAY_RADIUS := 40.0                 # m : balancement actif autour de la caméra
const LIGHT_RANGE := 7.0
const LIGHT_ENERGY := 1.3
const LIGHT_COLOR := Color(1.0, 0.68, 0.38)
const FADE_BEGIN := 22.0                  # m : la lumière s'éteint progressivement (perf iGPU)
const FADE_LENGTH := 10.0
const INDOOR_MIN_Y := 3000.0

var _lights: Array = []        # {light, base, outdoor}
var _sway: Array = []          # {mi, base, pivot, gpos, amp, freq, phase}
var _bodies: Array = []        # {body, home, state} state : 0 figé, 1 libre, 2 refusé (pas de support)
var _halo: StandardMaterial3D
var _t := 0.0
var _acc := 0.0
var _sun: Light3D
var _mi_seen := {}

func setup(roots: Array) -> void:
	if OS.get_environment("CYBERREALM_NO_PROPS") == "1":
		return
	_sun = get_parent().get_parent().get_node_or_null("Sun") as Light3D
	_halo = StandardMaterial3D.new()
	_halo.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	_halo.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	_halo.blend_mode = BaseMaterial3D.BLEND_MODE_ADD
	_halo.albedo_color = Color(0.0, 0.0, 0.0, 1.0)
	_halo.emission_enabled = true
	_halo.emission = Color(1.0, 0.55, 0.2)
	_halo.emission_energy_multiplier = 0.5
	for r in roots:
		if r == null:
			continue
		for n in (r as Node).find_children("*", "MeshInstance3D", true, false):
			_process_mesh(n as MeshInstance3D)

static func _has_word(s: String, words: Array) -> bool:
	for w in words:
		if s.contains(w):
			return true
	return false

func _has_collider(mi: MeshInstance3D) -> bool:
	var par := mi.get_parent()
	for i in range(mi.get_index() + 1, mini(mi.get_index() + 3, par.get_child_count())):
		var sb := par.get_child(i) as StaticBody3D
		if sb != null and sb.global_position.distance_to(mi.global_position) < 0.05:
			return true
	return false

func _process_mesh(mi: MeshInstance3D) -> void:
	if mi.mesh == null or _mi_seen.has(mi):
		return
	_mi_seen[mi] = true
	var low := String(mi.name).to_lower()
	if _has_word(low, GLOW_WORDS) and not low.contains("-colonly"):
		_add_glow(mi, low)
		if _has_word(low, SWAY_WORDS):
			_add_sway(mi, low)
	elif _has_word(low, PHYS_WORDS) and not _has_word(low, NOT_PHYS_WORDS):
		_add_rigid(mi)

# ── Lumière ──────────────────────────────────────────────────────────

func _add_glow(mi: MeshInstance3D, low: String) -> void:
	var light := OmniLight3D.new()
	light.name = "PropLight"
	light.light_color = LIGHT_COLOR
	light.light_energy = LIGHT_ENERGY
	light.omni_range = LIGHT_RANGE * (1.4 if low.contains("lightpost") else 1.0)
	light.shadow_enabled = false
	light.distance_fade_enabled = true
	light.distance_fade_begin = FADE_BEGIN
	light.distance_fade_length = FADE_LENGTH
	light.light_cull_mask = mi.layers | 1       # couche du mesh (extérieur / intérieur) + avatars
	light.position = mi.get_aabb().get_center()
	mi.set_meta("lan_skip_children", true)
	mi.add_child(light)
	if not _has_word(low, NO_HALO_WORDS):
		mi.material_overlay = _halo
	_lights.append({"light": light, "base": LIGHT_ENERGY, "outdoor": mi.global_position.y < INDOOR_MIN_Y})

# Extérieur : allumées quand le soleil/la lune est faible. Appelé quelques fois par seconde.
func _update_lights() -> void:
	var dark := 1.0
	if _sun != null:
		dark = 1.0 - smoothstep(0.1, 0.9, _sun.light_energy)
	for d in _lights:
		var l := d.light as OmniLight3D
		if not is_instance_valid(l):
			continue
		if d.outdoor:
			l.visible = dark > 0.02
			l.light_energy = d.base * dark
		else:
			l.visible = true

# ── Balancement ──────────────────────────────────────────────────────

func _add_sway(mi: MeshInstance3D, low: String) -> void:
	var aabb := mi.get_aabb()
	_sway.append({
		"mi": mi, "base": mi.transform,
		"pivot": Vector3(aabb.get_center().x, aabb.end.y, aabb.get_center().z),   # point d'accroche : haut du mesh
		"gpos": mi.global_position,
		"amp": deg_to_rad(5.0 if low.contains("sway") else 2.5),
		"freq": randf_range(1.3, 1.9), "phase": randf() * TAU})

func _update_sway() -> void:
	var cam := get_viewport().get_camera_3d()
	if cam == null:
		return
	var cp := cam.global_position
	for d in _sway:
		var mi := d.mi as MeshInstance3D
		if not is_instance_valid(mi) or (d.gpos as Vector3).distance_squared_to(cp) > SWAY_RADIUS * SWAY_RADIUS \
				or not mi.is_visible_in_tree():
			continue
		var a: float = d.amp * (sin(_t * d.freq + d.phase) + 0.4 * sin(_t * d.freq * 2.3 + d.phase * 1.7))
		var b: float = d.amp * 0.7 * sin(_t * d.freq * 0.83 + d.phase * 2.1)
		var rot := Basis(Vector3.RIGHT, a) * Basis(Vector3.BACK, b)
		var p: Vector3 = d.pivot
		mi.transform = (d.base as Transform3D) * Transform3D(Basis.IDENTITY, p) * Transform3D(rot, Vector3.ZERO) \
				* Transform3D(Basis.IDENTITY, -p)

# ── Objets physiques ─────────────────────────────────────────────────

func _add_rigid(mi: MeshInstance3D) -> void:
	var sc := mi.global_transform.basis.get_scale().abs()
	var size := mi.get_aabb().size * sc
	var ext := maxf(size.x, maxf(size.y, size.z))
	if ext < PHYS_MIN_SIZE or ext > PHYS_MAX_SIZE or _has_collider(mi):
		return
	var shape := mi.mesh.create_convex_shape(true, true)
	if shape == null or shape.points.size() < 4:
		return
	var pts := shape.points
	for i in pts.size():
		pts[i] *= sc
	shape.points = pts
	var body := RigidBody3D.new()
	body.set_script(PropBodyScript)
	body.set("mesh_node", mi)
	body.name = "PropBody"
	body.mass = clampf(size.x * size.y * size.z * 600.0, 0.1, 4.0)
	body.freeze = true                              # figé tant qu'on n'est pas proche (et qu'un support existe)
	body.freeze_mode = RigidBody3D.FREEZE_MODE_STATIC
	body.linear_damp = 0.4
	body.angular_damp = 1.5
	var cs := CollisionShape3D.new()
	cs.shape = shape
	body.add_child(cs)
	var par := mi.get_parent()
	par.set_meta("lan_skip_children", true)
	par.add_child(body)
	body.global_transform = Transform3D(mi.global_transform.basis.orthonormalized(), mi.global_position)
	# Le mesh d'origine reste en place dans l'arbre (donc baké normalement) et suit le corps.
	var rt := RemoteTransform3D.new()
	rt.update_scale = false
	body.add_child(rt)
	rt.remote_path = rt.get_path_to(mi)
	_bodies.append({"body": body, "home": body.global_transform, "state": 0})

func _wake_bodies() -> void:
	var cam := get_viewport().get_camera_3d()
	if cam == null:
		return
	var cp := cam.global_position
	for d in _bodies:
		if not is_instance_valid(d.body):
			continue
		var body := d.body as RigidBody3D
		if d.state != 1 and not body.freeze:      # pris en main : désormais libre
			d.state = 1
		if d.state == 1:
			if body.global_position.y < (d.home as Transform3D).origin.y - 15.0:   # traversé le décor : on le remet
				body.freeze = true
				body.global_transform = d.home
				d.state = 2
			continue
		if d.state != 0 or body.global_position.distance_squared_to(cp) > WAKE_RADIUS * WAKE_RADIUS:
			continue
		if not body.is_inside_tree() or not body.can_process():
			continue
		var from := body.global_position + Vector3.UP * 0.05
		var q := PhysicsRayQueryParameters3D.create(from, from + Vector3.DOWN * 0.3, 1, [body.get_rid()])
		var hit := body.get_world_3d().direct_space_state.intersect_ray(q)
		if hit.is_empty():
			d.state = 2                              # rien dessous (meuble sans collider) : reste du décor
		else:
			body.freeze = false
			d.state = 1

func _process(delta: float) -> void:
	_t += delta
	if not _sway.is_empty():
		_update_sway()

func _physics_process(delta: float) -> void:
	_acc += delta
	if _acc < 0.25:
		return
	_acc = 0.0
	_update_lights()
	_wake_bodies()
