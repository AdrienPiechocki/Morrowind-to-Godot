extends Node
## Halo (glow) du soleil dessiné APRÈS les fenêtres.
##
## Le glow du moteur (bloom) dépend de la luminosité du disque solaire dans le ciel : une fenêtre
## semi-transparente l'assombrit et le halo disparaît. Ici le halo est une couche additive plein écran,
## dessinée après les fenêtres : il ne dépend plus d'elles. Elle ne s'affiche que sur les pixels de ciel
## (tampon de profondeur vide, test de profondeur matériel) : le relief, les bâtiments et les fenêtres
## opaques (qui écrivent leur profondeur) le masquent ; les fenêtres translucides le laissent passer.
## Pas d'affichage à l'intérieur (caméra avec environnement propre).

@export_range(0.0, 3.0, 0.05) var glare_intensity := 1.0   # 0 = aucun halo ; réglé dans le menu pause > Level

const SHADER_CODE := """
shader_type spatial;
render_mode unshaded, blend_add, cull_disabled, depth_draw_never, shadows_disabled, fog_disabled;

uniform vec3 sun_dir = vec3(0.0, 1.0, 0.0);   // direction VERS le soleil
uniform float strength = 1.0;

void vertex() {
	// Quad plein ecran (QuadMesh 2x2) tout au fond (profondeur ~0, Z inverse) AVEC test de profondeur : le halo
	// n'apparait que sur les pixels dont le tampon de profondeur est encore vide (ciel), y compris derriere les
	// fenetres OPAQUES (qui ecrivent leur profondeur), tandis que les fenetres translucides le laissent passer.
	POSITION = vec4(VERTEX.x, VERTEX.y, 0.0000001, 1.0);
}

void fragment() {
	vec4 vp = INV_PROJECTION_MATRIX * vec4(SCREEN_UV * 2.0 - 1.0, 0.5, 1.0);
	vec3 rd = normalize((INV_VIEW_MATRIX * vec4(vp.xyz / vp.w, 0.0)).xyz);
	float c = dot(rd, sun_dir);
	if (c <= 0.0) discard;
	float ang = acos(clamp(c, -1.0, 1.0));
	// somme de gaussiennes (angle en radians) : coeur, halo, lueur large
	float g = 1.4 * exp(-pow(ang / 0.012, 2.0))
			+ 0.55 * exp(-pow(ang / 0.05, 2.0))
			+ 0.18 * exp(-pow(ang / 0.22, 2.0))
			+ 0.05 * exp(-pow(ang / 0.7, 2.0));
	vec3 col = mix(vec3(1.0, 0.55, 0.25), vec3(1.0, 0.96, 0.85), clamp(sun_dir.y * 3.0, 0.0, 1.0));
	ALBEDO = col * g * strength;
	ALPHA = 1.0;
}
"""

var _mat: ShaderMaterial
var _mi: MeshInstance3D
var _dn: Node

func _ready() -> void:
	set_meta("lan_skip_children", true)   # quad recréé par chaque machine (pas baké)
	_dn = get_parent().get_node_or_null("DayNight")
	var sh := Shader.new()
	sh.code = SHADER_CODE
	_mat = ShaderMaterial.new()
	_mat.shader = sh
	_mat.render_priority = 120            # après toutes les fenêtres (priorités 0..100)
	var q := QuadMesh.new()
	q.size = Vector2(2, 2)
	_mi = MeshInstance3D.new()
	_mi.name = "Glare"
	_mi.mesh = q
	_mi.material_override = _mat
	_mi.extra_cull_margin = 16384.0
	_mi.ignore_occlusion_culling = true
	_mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	_mi.visible = false
	add_child(_mi)

func _process(_delta: float) -> void:
	var cam := get_viewport().get_camera_3d()
	var sky = _dn.get("_sky_mat") if _dn != null else null
	if cam == null or sky == null or glare_intensity <= 0.0 or cam.environment != null:
		_mi.visible = false
		return
	var sun_dir = sky.get_shader_parameter("sun_dir")
	if not (sun_dir is Vector3):
		_mi.visible = false
		return
	var sun_h: float = (sun_dir as Vector3).y
	var vis := smoothstep(-0.06, 0.02, sun_h)
	var overcast := float(_dn.get("overcast"))
	var cloud := float(_dn.get("cloud_density")) if _dn.get("cloud_density") != null else 0.0
	var strength := glare_intensity * vis * (1.0 - overcast * 0.85) * (1.0 - 0.35 * cloud)
	_mi.visible = strength > 0.001
	_mat.set_shader_parameter("sun_dir", sun_dir)
	_mat.set_shader_parameter("strength", strength)
