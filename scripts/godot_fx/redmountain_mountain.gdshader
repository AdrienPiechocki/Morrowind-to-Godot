shader_type spatial;
render_mode unshaded, cull_disabled, depth_draw_never, shadows_disabled, fog_disabled, blend_mix;
// Impostor du Mont Ecarlate : silhouette (masque alpha) choisie parmi 64 vues autour de la boussole,
// sur un plan vertical qui fait toujours face a la camera (axe vertical bloque).
// Utile quand la vraie montagne n'est pas dans la scene (hors zone de terrain).
// Dessine APRES le panache (render_priority) : la montagne est devant la fumee, qui sort de derriere elle.

uniform float size = 896.87;                 // cote du plan (m)
uniform vec3 tint : source_color = vec3(0.243, 0.380, 0.596);
uniform vec3 fog_color = vec3(0.58, 0.72, 0.88);
uniform float haze_density = 0.00035;
uniform float haze_max = 0.76;               // brume maximale (garde une silhouette tres douce)
uniform float brightness = 1.0;
uniform float light = 1.0;                   // 0 nuit .. 1 jour
uniform float presence : hint_range(0.0, 1.0) = 0.5;
uniform float fade_start = 1500.0;           // m : opacite pleine en dessous
uniform float fade_end = 7000.0;             // m : invisible au-dela
uniform sampler2D atlas0 : filter_linear_mipmap, repeat_disable;
uniform sampler2D atlas1 : filter_linear_mipmap, repeat_disable;
uniform sampler2D atlas2 : filter_linear_mipmap, repeat_disable;
uniform sampler2D atlas3 : filter_linear_mipmap, repeat_disable;

varying vec3 n_flat;      // horizontale : de la montagne vers la camera
varying vec2 quad_uv;     // 0..1, v vers le haut
varying float dist_cam;

const int ANGLES = 64;

float sprite(int angle, vec2 suv) {
	int s = angle % 16;
	vec2 cell = vec2(float(s % 4), float(s / 4));
	vec2 uv = (cell + suv) / 4.0;
	int atl = angle / 16;
	if (atl == 0) return texture(atlas0, uv).r;
	if (atl == 1) return texture(atlas1, uv).r;
	if (atl == 2) return texture(atlas2, uv).r;
	return texture(atlas3, uv).r;
}

void vertex() {
	vec3 center = MODEL_MATRIX[3].xyz;
	vec3 cam = (INV_VIEW_MATRIX * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
	vec3 to_cam = cam - center;
	to_cam.y = 0.0;
	vec3 n = normalize(to_cam + vec3(1e-6, 0.0, 0.0));
	vec3 right = normalize(cross(vec3(0.0, 1.0, 0.0), n));      // vue du sud : l'est a droite
	vec3 world = center + right * VERTEX.x * size + vec3(0.0, 1.0, 0.0) * VERTEX.y * size;
	n_flat = n;
	quad_uv = VERTEX.xy + 0.5;
	dist_cam = length(cam - center);
	POSITION = PROJECTION_MATRIX * (VIEW_MATRIX * vec4(world, 1.0));
}

void fragment() {
	// angle de la camera autour de la montagne : nord = 0, croissant vers l'est (nord = -z monde)
	float a = atan(n_flat.x, -n_flat.z);
	float sector = 6.28318530718 / float(ANGLES);
	float f = mod(a / sector, float(ANGLES));
	int k = int(f);
	int k2 = (k + 1) % ANGLES;
	float w = clamp((fract(f) - 0.5) / 0.1 + 0.5, 0.0, 1.0);     // fondu etroit entre deux vues voisines
	vec2 suv = vec2(quad_uv.x, 1.0 - quad_uv.y);
	float m = mix(sprite(k, suv), sprite(k2, suv), w);
	if (m < 0.01) discard;
	float haze = 1.0 - exp(-dist_cam * haze_density);
	// le bas de l'image (pied de la montagne) se fond dans la brume du monde, sans bord visible
	float low = smoothstep(0.55, 0.40, quad_uv.y);
	vec3 col = tint * brightness * (0.25 + 0.75 * light);
	// meme brume exponentielle que le terrain (Environment.fog_density) : la montagne se fond comme lui
	col = mix(col, fog_color, clamp(max(haze, low), 0.0, haze_max + (1.0 - haze_max) * low));
	ALBEDO = col;
	float fade = pow(1.0 - smoothstep(fade_start, fade_end, dist_cam), 1.5);     // plus loin = plus transparent
	ALPHA = smoothstep(0.02, 0.45, m) * fade;      // opaque : cache la fumee derriere elle
}
