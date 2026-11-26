extends Node
## Options du niveau dans le menu pause (bouton « Level » du menu principal).
##
## Modifiables par l'HÔTE (ou en solo) ; les autres joueurs voient les valeurs
## en lecture seule. Chaque changement est appliqué localement puis diffusé aux
## clients (RPC "authority" : seul l'hôte peut en émettre). L'état vit dans les
## nœuds du niveau (DayNight.real_time / time_of_day, Weather.raining), donc un
## joueur qui rejoint le reçoit déjà avec le niveau baké.
##
## Sur un SERVEUR DÉDIÉ (aucun joueur n'est l'hôte), les réglages sont réservés aux joueurs ADMIN (mot de passe du
## serveur, voir lan_manager.is_admin) : le client envoie sa demande au serveur (message « lvlopt_set »), qui la
## valide, la mémorise et la diffuse à tous (« lvlopt »). Un joueur qui arrive demande l'état courant (« lvlopt_get »).
##
## Entrée « Level » dans la page World du menu pause via son API add_menu_page ;
## repli pour un menu pause plus ancien : bouton greffé à la page principale à
## chaque reconstruction de celle-ci. Tout est retiré à la sortie de l'arbre
## (changement de niveau).

enum { LOCAL, SERVER, CLIENT }
var _net := LOCAL
var _lan: Node = null
var _server_state: Dictionary = {}     # SERVEUR : dernier état connu des options
var _admin_status: Label = null
var _admin_pw: LineEdit = null

const BTN_META := "level_options_btn"
const BROADCAST_DELAY := 0.25     # regroupe les rafales (curseur) en un seul envoi

var _pm: Node = null              # PauseMenu du joueur local
var _btn: Button = null
var _dirty := -1.0
var _time_mode: OptionButton = null
var _hour_slider: HSlider = null
var _hour_label: Label = null
var _rain_btn: CheckButton = null
var _fog_slider: HSlider = null
var _fog_label: Label = null
var _cloud_slider: HSlider = null
var _cloud_label: Label = null
var _info: Label = null

func _ready() -> void:
	_lan = get_node_or_null("/root/WaylandRoom/LAN")
	if _lan == null:
		return
	if _lan.get("dedicated") == true:
		_net = SERVER
		set_process(false)       # pas de menu pause sur un serveur
		_server_state = _state_from_nodes()
		_lan.level_message.connect(_on_server_message)
	elif _lan.get("session_dedicated") == true:
		_net = CLIENT
		_lan.level_message.connect(_on_client_message)
		_lan.admin_changed.connect(_on_admin_changed)
		_lan.send_level_message("lvlopt_get")      # état courant des options (réglé par un admin avant mon arrivée)

func _process(delta: float) -> void:
	if _pm == null:
		_try_attach()
	if _dirty >= 0.0:
		_dirty -= delta
		if _dirty < 0.0:
			_broadcast()

func _exit_tree() -> void:
	if _pm != null and is_instance_valid(_pm) and _pm.has_method("remove_menu_page"):
		_pm.call("remove_menu_page", "level_options")
		return
	if _pm != null and is_instance_valid(_pm):
		var c := _pm.get("container") as Container
		if c != null and c.child_order_changed.is_connected(_on_container_changed):
			c.child_order_changed.disconnect(_on_container_changed)
		if _btn != null and is_instance_valid(_btn):
			_btn.queue_free()
		# Si la page « Level » est affichée, revenir au menu principal.
		if _pm.get("_current_view") == "level_options" and _pm.visible:
			_pm.call("_show_main")

func _try_attach() -> void:
	var player := get_parent().find_child("Player", true, false)
	var pm := player.get_node_or_null("PauseMenuLayer/PauseMenu") if player != null else null
	if pm == null or pm.get("container") == null:
		return
	_pm = pm
	if pm.has_method("add_menu_page"):
		pm.call("add_menu_page", "level_options", "Level", _show_page)
		return
	var c := pm.get("container") as Container
	c.child_order_changed.connect(_on_container_changed)
	_on_container_changed()

func _on_container_changed() -> void:
	_inject.call_deferred()

# ── Greffe sur le menu principal ─────────────────────────────────────

func _inject() -> void:
	if _pm == null or not is_instance_valid(_pm) or _pm.get("_current_view") != "main":
		return
	var c := _pm.get("container") as Container
	for ch in c.get_children():
		if ch.has_meta(BTN_META) and not ch.is_queued_for_deletion():
			return
	_btn = _pm.call("_make_btn", "Level") as Button
	_btn.set_meta(BTN_META, true)
	_btn.pressed.connect(_show_page)
	c.add_child(_btn)
	# Avant le ressort (premier enfant non-bouton qui prend la hauteur libre).
	var idx := c.get_child_count() - 1
	for i in c.get_child_count() - 1:
		var ch := c.get_child(i)
		# Les enfants de la page précédente (queue_free en attente) sont encore là.
		if ch.is_queued_for_deletion():
			continue
		if not (ch is BaseButton) and ch is Control \
				and (ch as Control).size_flags_vertical & Control.SIZE_EXPAND:
			idx = i
			break
	c.move_child(_btn, idx)

# ── Page « Level » ───────────────────────────────────────────────────

func _is_host() -> bool:
	# « Peut modifier » : en solo / hôte LAN, l'hôte ; sur serveur dédié, un joueur admin.
	if _net == CLIENT:
		return bool(_lan.call("is_admin"))
	return get_tree().get_multiplayer().is_server()

func _nodes() -> Dictionary:
	var lvl := get_parent()
	return {"dn": lvl.get_node_or_null("DayNight"), "weather": lvl.get_node_or_null("Weather")}

func _show_page() -> void:
	_pm.call("_clear")
	_pm.set("_current_view", "level_options")
	var c := _pm.get("container") as Container
	var host := _is_host()
	var n := _nodes()
	var dn: Node = n["dn"]
	var weather: Node = n["weather"]
	c.add_child(_pm.call("_make_title", "LEVEL OPTIONS"))
	if not host:
		_info = Label.new()
		_info.text = "Only an admin can change the level options." if _net == CLIENT \
				else "Only the host can change the level options."
		_info.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		_info.add_theme_font_size_override("font_size", 14)
		c.add_child(_info)
		if _net == CLIENT:
			_add_admin_login(c)

	# ── Heure ──
	c.add_child(_pm.call("_make_row_label", "Time of day"))
	_time_mode = OptionButton.new()
	_time_mode.add_item("Real time (local clock)", 0)
	_time_mode.add_item("Fixed hour", 1)
	_time_mode.custom_minimum_size = Vector2(0, 36)
	_time_mode.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_time_mode.add_theme_font_size_override("font_size", 14)
	_hour_label = Label.new()
	_hour_label.add_theme_font_size_override("font_size", 14)
	_hour_slider = HSlider.new()
	_hour_slider.min_value = 0.0
	_hour_slider.max_value = 23.75
	_hour_slider.step = 0.25
	_hour_slider.custom_minimum_size = Vector2(0, 28)
	_hour_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	if dn != null:
		_time_mode.selected = 0 if dn.get("real_time") else 1
		_hour_slider.value = snappedf(float(dn.get("time_of_day")), 0.25)
	_update_hour_label()
	c.add_child(_time_mode)
	c.add_child(_hour_label)
	c.add_child(_hour_slider)

	# ── Brume ──
	c.add_child(_pm.call("_make_row_label", "Fog thickness"))
	_fog_label = Label.new()
	_fog_label.add_theme_font_size_override("font_size", 14)
	_fog_slider = HSlider.new()
	_fog_slider.min_value = 0.0
	_fog_slider.max_value = 5.0
	_fog_slider.step = 0.1
	_fog_slider.custom_minimum_size = Vector2(0, 28)
	_fog_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_fog_slider.value = float(dn.get("fog_thickness")) if dn != null and dn.get("fog_thickness") != null else 1.0
	_update_fog_label()
	c.add_child(_fog_label)
	c.add_child(_fog_slider)

	# ── Nuages ──
	c.add_child(_pm.call("_make_row_label", "Cloud density"))
	_cloud_label = Label.new()
	_cloud_label.add_theme_font_size_override("font_size", 14)
	_cloud_slider = HSlider.new()
	_cloud_slider.min_value = 0.0
	_cloud_slider.max_value = 1.0
	_cloud_slider.step = 0.05
	_cloud_slider.custom_minimum_size = Vector2(0, 28)
	_cloud_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_cloud_slider.value = float(dn.get("cloud_density")) if dn != null and dn.get("cloud_density") != null else 0.43
	_update_cloud_label()
	c.add_child(_cloud_label)
	c.add_child(_cloud_slider)

	# ── Pluie ──
	_rain_btn = CheckButton.new()
	_rain_btn.text = "Rain"
	_rain_btn.custom_minimum_size = Vector2(0, 36)
	_rain_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_rain_btn.add_theme_font_size_override("font_size", 14)
	_rain_btn.button_pressed = weather != null and bool(weather.get("raining"))
	c.add_child(_rain_btn)

	_refresh_enabled()
	if host:
		_fog_slider.value_changed.connect(_on_changed)
		_cloud_slider.value_changed.connect(_on_changed)
		_time_mode.item_selected.connect(_on_changed)
		_hour_slider.value_changed.connect(_on_changed)
		_rain_btn.toggled.connect(_on_changed)
	c.add_child(_pm.call("_make_spacer"))
	c.add_child(_pm.call("_make_back_btn"))

# Connexion admin (serveur dédié) : mot de passe -> demande au serveur ; admin_changed rebâtit la page.
func _add_admin_login(c: Container) -> void:
	_admin_pw = LineEdit.new()
	_admin_pw.secret = true
	_admin_pw.placeholder_text = "Admin password"
	_admin_pw.custom_minimum_size = Vector2(0, 36)
	_admin_pw.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_admin_pw.add_theme_font_size_override("font_size", 14)
	c.add_child(_admin_pw)
	var b := _pm.call("_make_btn", "Unlock as admin") as Button
	b.pressed.connect(_submit_admin_password)
	_admin_pw.text_submitted.connect(func(_t: String): _submit_admin_password())
	c.add_child(b)
	_admin_status = Label.new()
	_admin_status.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_admin_status.add_theme_font_size_override("font_size", 13)
	c.add_child(_admin_status)

func _submit_admin_password() -> void:
	if _admin_pw == null or _admin_pw.text.is_empty():
		return
	if _admin_status != null:
		_admin_status.text = "Checking…"
	_lan.call("request_admin", _admin_pw.text)
	_admin_pw.text = ""

func _on_admin_changed(is_admin: bool) -> void:
	if _pm == null or not is_instance_valid(_pm) or _pm.get("_current_view") != "level_options":
		return
	if is_admin:
		_show_page()           # la page se rebâtit éditable
	elif _admin_status != null:
		_admin_status.text = "Wrong password (or too many attempts: wait 30 s)."

func _refresh_enabled() -> void:
	var host := _is_host()
	_time_mode.disabled = not host
	_rain_btn.disabled = not host
	_cloud_slider.editable = host
	_cloud_slider.modulate.a = 1.0 if host else 0.5
	# Brume : automatique (selon l'heure) en heure réelle ; curseur seulement en heure fixe.
	_fog_slider.editable = host and _time_mode.selected == 1
	_fog_slider.modulate.a = 1.0 if _fog_slider.editable else 0.5
	_hour_slider.editable = host and _time_mode.selected == 1
	_hour_slider.modulate.a = 1.0 if _hour_slider.editable else 0.5

func _update_hour_label() -> void:
	var h := _hour_slider.value
	if _time_mode.selected == 0:
		var d := Time.get_datetime_dict_from_system()
		_hour_label.text = "Now: %02d:%02d" % [d.hour, d.minute]
	else:
		_hour_label.text = "Hour: %02d:%02d" % [int(h), int(round((h - floorf(h)) * 60.0))]

func _update_fog_label() -> void:
	if _time_mode.selected == 0:
		_fog_label.text = "Fog: automatic (follows the time of day)"
	else:
		_fog_label.text = "Fog: x%.1f%s" % [_fog_slider.value, " (off)" if _fog_slider.value <= 0.0 else ""]

func _update_cloud_label() -> void:
	_cloud_label.text = "Clouds: %d%%" % int(round(_cloud_slider.value * 100.0))

func _on_changed(_v = null) -> void:
	_update_cloud_label()
	_refresh_enabled()
	_update_hour_label()
	_update_fog_label()
	_apply(_state_from_ui())
	_dirty = BROADCAST_DELAY

func _state_from_ui() -> Dictionary:
	return {"fixed": _time_mode.selected == 1, "hour": _hour_slider.value, "rain": _rain_btn.button_pressed,
		"fog": _fog_slider.value,
		"cloud": _cloud_slider.value}

# ── Application + réseau ─────────────────────────────────────────────

func _apply(state: Dictionary) -> void:
	var n := _nodes()
	var dn: Node = n["dn"]
	var weather: Node = n["weather"]
	if dn != null:
		var fixed := bool(state.get("fixed", false))
		dn.set("real_time", not fixed)
		dn.set("day_length", 0.0)
		if fixed:
			dn.set("time_of_day", clampf(float(state.get("hour", 12.0)), 0.0, 24.0))
	if dn != null and state.has("fog"):
		dn.set("fog_thickness", clampf(float(state["fog"]), 0.0, 5.0))
	if dn != null and state.has("cloud"):
		dn.set("cloud_density", clampf(float(state["cloud"]), 0.0, 1.0))
	if weather != null:
		weather.set("raining", bool(state.get("rain", false)))

func _broadcast() -> void:
	_dirty = -1.0
	if _net == CLIENT:
		if bool(_lan.call("is_admin")):
			_lan.send_level_message("lvlopt_set", _state_from_ui())
		return
	if not _is_host() or get_tree().get_multiplayer().get_peers().is_empty():
		return
	_net_apply.rpc(_state_from_ui())

# ── Serveur dédié ────────────────────────────────────────────────────

# État initial du serveur : celui des nœuds du niveau tels que bakés (leurs scripts sont retirés sur un serveur :
# les variables exportées sont alors dans la méta « script_props », cf. dedicated_server.gd).
static func _prop(node: Node, key: String, default: Variant) -> Variant:
	if node == null:
		return default
	var sp: Dictionary = node.get_meta("script_props", {})
	var v = sp.get(key, node.get(key))
	return default if v == null else v

func _state_from_nodes() -> Dictionary:
	var n := _nodes()
	var dn: Node = n["dn"]
	var weather: Node = n["weather"]
	return {
		"fixed": not bool(_prop(dn, "real_time", true)),
		"hour": float(_prop(dn, "time_of_day", 12.0)),
		"fog": float(_prop(dn, "fog_thickness", 1.0)),
		"cloud": float(_prop(dn, "cloud_density", 0.43)),
		"rain": bool(_prop(weather, "raining", false))}

# Valide un état reçu (jamais confiance au réseau) et le ramène aux seuls champs/bornes connus.
static func _sanitize(state: Variant) -> Dictionary:
	var out := {}
	if not (state is Dictionary):
		return out
	var d: Dictionary = state
	out["fixed"] = d.get("fixed", false) == true
	out["rain"] = d.get("rain", false) == true
	for k in [["hour", 12.0, 0.0, 24.0], ["fog", 1.0, 0.0, 5.0], ["cloud", 0.43, 0.0, 1.0]]:
		var v = d.get(k[0], k[1])
		out[k[0]] = clampf(float(v) if typeof(v) == TYPE_FLOAT or typeof(v) == TYPE_INT else k[1], k[2], k[3])
	return out

func _on_server_message(topic: String, data: Variant, from: int) -> void:
	match topic:
		"lvlopt_get":
			_lan.send_level_message_to(from, "lvlopt", _server_state)
		"lvlopt_set":
			if not bool(_lan.call("is_admin", from)):
				return
			var st := _sanitize(data)
			if st.is_empty():
				return
			_server_state = st
			_lan.broadcast_level_message("lvlopt", st)

func _on_client_message(topic: String, data: Variant, _from: int) -> void:
	if topic != "lvlopt":
		return
	var st := _sanitize(data)
	if st.is_empty():
		return
	_apply(st)
	# Page ouverte : refléter la nouvelle valeur (sans renvoyer de changement).
	if _pm != null and is_instance_valid(_pm) and _pm.get("_current_view") == "level_options" \
			and _hour_slider != null and is_instance_valid(_hour_slider):
		_hour_slider.set_value_no_signal(float(st["hour"]))
		_time_mode.select(1 if st["fixed"] else 0)
		_fog_slider.set_value_no_signal(float(st["fog"]))
		_cloud_slider.set_value_no_signal(float(st["cloud"]))
		_rain_btn.set_pressed_no_signal(bool(st["rain"]))
		_update_hour_label(); _update_fog_label(); _update_cloud_label(); _refresh_enabled()

@rpc("authority", "call_remote", "reliable")
func _net_apply(state: Dictionary) -> void:
	# Garde-fous : un hôte malveillant ne peut envoyer que ces champs.
	var h = state.get("hour", 12.0)
	var f = state.get("fog", 1.0)
	var cl = state.get("cloud", 0.43)
	_apply({"fixed": state.get("fixed", false) == true,
		"hour": float(h) if typeof(h) == TYPE_FLOAT or typeof(h) == TYPE_INT else 12.0,
		"rain": state.get("rain", false) == true,
		"fog": float(f) if typeof(f) == TYPE_FLOAT or typeof(f) == TYPE_INT else 1.0,
		"cloud": float(cl) if typeof(cl) == TYPE_FLOAT or typeof(cl) == TYPE_INT else 0.43})
