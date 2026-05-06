import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'dart:math';
import 'dart:ui';
import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:geolocator/geolocator.dart';
import 'package:geocoding/geocoding.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:image_picker/image_picker.dart'; // requires: image_picker: ^1.0.4 in pubspec.yaml
import 'dart:io';
import 'package:razorpay_flutter/razorpay_flutter.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:shimmer/shimmer.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'firebase_options.dart';

// ─────────────────────── CONFIG ───────────────────────
const BASE_URL = "https://offro-backend-production.up.railway.app";
const RAZORPAY_KEY = "rzp_live_SdiI6kcuZzZjsl";

// ─────────────────────── COLORS ───────────────────────
const kPrimary  = Color(0xFF3E5F55);
const kLight    = Color(0xFFCDEBD6);
const kAccent   = Color(0xFFA9CDBA);
const kBeige    = Color(0xFFE7D7C8);
const kBg       = Color(0xFFFDFBF6);
const kText     = Color(0xFF2c3e35);
const kMuted    = Color(0xFF6b8c7e);
const kBorder   = Color(0xFFd4e8de);

// ─────────────────────── API ───────────────────────
class Api {
  static Map<String,String> _h(String? token) => {
    "Content-Type": "application/json",
    if (token != null) "Authorization": "Bearer $token",
  };

  static Future<Map<String,dynamic>> _post(String path, Map body, {String? token}) async {
    final r = await http.post(Uri.parse("$BASE_URL$path"), headers: _h(token), body: json.encode(body)).timeout(const Duration(seconds: 12));
    final d = json.decode(r.body);
    if (r.statusCode >= 400) throw Exception(d["detail"] ?? "Error ${r.statusCode}");
    return d;
  }

  static Future<dynamic> _get(String path, {String? token}) async {
    final r = await http.get(Uri.parse("$BASE_URL$path"), headers: _h(token)).timeout(const Duration(seconds: 12));
    if (r.statusCode >= 400) { final d = json.decode(r.body); throw Exception(d["detail"] ?? "Error"); }
    return json.decode(r.body);
  }

  // ── API Response Cache (30-second TTL) ──
  static final Map<String, dynamic> _apiCache = {};
  static final Map<String, DateTime> _apiCacheTime = {};
  static const _cacheTTL = const Duration(minutes: 3);

  static bool _isCacheValid(String key) {
    final t = _apiCacheTime[key];
    return t != null && DateTime.now().difference(t) < _cacheTTL;
  }

  static void clearCache() {
    _apiCache.clear();
    _apiCacheTime.clear();
  }

  static Future<dynamic> _put(String path, Map body, {String? token}) async {
    final r = await http.put(Uri.parse("$BASE_URL$path"), headers: _h(token), body: json.encode(body)).timeout(const Duration(seconds: 12));
    final d = json.decode(r.body);
    if (r.statusCode >= 400) throw Exception(d["detail"] ?? "Error");
    return d;
  }

  // ── User ──
  static Future<Map<String,dynamic>> loginUser(String phone) => _post("/user/login", {"phone": phone});
  static Future<Map<String,dynamic>> registerUser(String name, String phone) =>
      _post("/user/register", {"name": name, "phone": phone, "city": ""});
  static Future<void> updateCity(String token, String city) async {
    try { await _put("/user/city", {"city": city}, token: token); } catch (_) {}
  }
  static Future<Map<String,dynamic>> getWallet(String token) async => Map<String,dynamic>.from(await _get("/user/wallet", token: token));
  static Future<Map<String,dynamic>> withdraw(String token, int amount) async =>
      Map<String,dynamic>.from(await _post("/user/wallet/withdraw", {"amount": amount}, token: token));
  static Future<List> getRedemptions(String token) async {
    try { return await _get("/user/redemptions", token: token); } catch (_) { return []; }
  }
  static Future<Map<String,dynamic>> redeemQR(String storeId, String token) async =>
      Map<String,dynamic>.from(await _post("/user/redeem", {"store_id": storeId, "user_token": token}));

  // ── Merchant ──
  static Future<Map<String,dynamic>?> getMerchantMe(String token) async {
    try { return await _get("/merchant/me", token: token); } catch (_) { return null; }
  }
  static Future<Map<String,dynamic>> loginMerchant(String phone) => _post("/merchant/login", {"phone": phone});
  static Future<Map<String,dynamic>> registerMerchant(String name, String phone, String city, String area) =>
      _post("/merchant/register", {"name": name, "phone": phone, "city": city, "area": area});
  static Future<List> getMerchantStores(String token) async {
    try { return await _get("/merchant/stores", token: token); } catch (_) { return []; }
  }
  static Future<Map<String,dynamic>?> getMerchantStoreDetail(String token, String storeId) async {
    try {
      final d = await _get("/merchant/stores/$storeId", token: token);
      return d is Map ? Map<String,dynamic>.from(d) : null;
    } catch (_) { return null; }
  }
  static Future<Map<String,dynamic>> createMerchantStore(String token, Map<String,dynamic> data) =>
      _post("/merchant/stores", data, token: token);
  static Future<Map<String,dynamic>> updateMerchantStore(String token, String sid, Map<String,dynamic> data) async {
    final r = await http.put(Uri.parse("$BASE_URL/merchant/stores/$sid"), headers: _h(token), body: json.encode(data)).timeout(const Duration(seconds: 12));
    final d = json.decode(r.body);
    if (r.statusCode >= 400) throw Exception(d["detail"] ?? "Error");
    return d;
  }
  static Future<List> getPlans(String token) async {
    try { return await _get("/merchant/plans", token: token); } catch (_) { return []; }
  }
  static Future<Map<String,dynamic>> initiateSubscription(String token, Map body) =>
      _post("/merchant/subscribe", body, token: token);
  static Future<Map<String,dynamic>> verifyPayment(String token, Map body) =>
      _post("/merchant/subscribe/verify", body, token: token);
  static Future<Map<String,dynamic>> activateFreeSubscription(String token, Map body) =>
      _post("/merchant/subscribe/free", body, token: token);
  static Future<List> getInvoices(String token) async {
    try { return await _get("/merchant/invoices", token: token); } catch (_) { return []; }
  }
  static Future<List> getMerchantTransactions(String token) async {
    try { return await _get("/merchant/transactions", token: token); } catch (_) { return []; }
  }
  static Future<String> getMerchantTerms() async {
    try { final d = await _get("/merchant/terms"); return d["content"] ?? ""; } catch (_) { return ""; }
  }
  static Future<List> getMerchantDeals(String token) async {
    try { return await _get("/merchant/deals", token: token); } catch (_) { return []; }
  }
  static Future<Map<String,dynamic>> addDeal(String token, Map<String,dynamic> data) =>
      _post("/merchant/deals", data, token: token);
  static Future<void> deleteDeal(String token, String dealId) async {
    try {
      final r = await http.delete(Uri.parse("$BASE_URL/merchant/deals/$dealId"), headers: _h(token)).timeout(const Duration(seconds: 12));
      if (r.statusCode >= 400) { final d = jsonDecode(r.body); throw Exception(d["detail"] ?? "Error"); }
    } catch (_) {}
  }

  // ── Public ──
  static Future<Map<String,dynamic>> fetchStoreDetail(String storeId) async =>
      Map<String,dynamic>.from(await _get("/stores/$storeId"));

  static Future<List> fetchStores({String? city, String? category}) async {
    String url = "/stores";
    List<String> p = [];
    if (city != null && city.isNotEmpty && city != "Detecting...") p.add("city=${Uri.encodeComponent(city)}");
    if (category != null && category != "All") p.add("category=${Uri.encodeComponent(category)}");
    if (p.isNotEmpty) url += "?" + p.join("&");
    final cacheKey = "stores:$url";
    if (_isCacheValid(cacheKey)) return List.from(_apiCache[cacheKey]);
    try {
      final result = await _get(url);
      _apiCache[cacheKey] = result;
      _apiCacheTime[cacheKey] = DateTime.now();
      return result;
    } catch (_) { return []; }
  }
  static Future<List<String>> fetchCategories() async {
    const cacheKey = "categories";
    if (_isCacheValid(cacheKey)) return List<String>.from(_apiCache[cacheKey]);
    try {
      final result = List<String>.from(await _get("/categories"));
      _apiCache[cacheKey] = result;
      _apiCacheTime[cacheKey] = DateTime.now();
      return result;
    } catch (_) { return ["Grocery","Restaurant","Pharmacy","Electronics","Clothing","Bakery","Salon"]; }
  }
  static Future<String> fetchTerms(String type) async {
    try { final d = await _get("/terms/$type"); return d["content"] ?? ""; } catch (_) { return ""; }
  }
  static Future<String> fetchPolicy(String type) async {
    try { final d = await _get("/policy/$type"); return d["content"] ?? ""; } catch (_) { return ""; }
  }
  static Future<Map<String,dynamic>> getSocialLinks() async {
    try { return Map<String,dynamic>.from(await _get("/social")); } catch(_){ return {}; }
  }
  static Future<Map<String,dynamic>?> getMe(String token) async {
    try { return Map<String,dynamic>.from(await _get("/user/me", token: token)); } catch(_) { return null; }
  }

  static Future<Map<String,dynamic>> updateUserProfile(String token, Map<String,dynamic> data) async {
    try { return Map<String,dynamic>.from(await _put("/user/profile", data, token: token)); } catch(e) { throw Exception(e.toString().replaceAll("Exception: ","")); }
  }
  static Future<Map<String,dynamic>> updateMerchantProfile(String token, Map<String,dynamic> data) async {
    try { return Map<String,dynamic>.from(await _put("/merchant/profile", data, token: token)); } catch(e) { throw Exception(e.toString().replaceAll("Exception: ","")); }
  }
  static Future<String> getAboutUs() async {
    try { final d = await _get("/about"); return d["content"] ?? ""; } catch (_) { return ""; }
  }
  // ── Gift Vouchers ──
  static Future<List> getGiftVouchers() async {
    try { return await _get("/gift-vouchers"); } catch(_) { return []; }
  }
  static Future<List> getSliders() async {
    try { return await _get("/sliders"); } catch(_) { return []; }
  }

  static Future<Map<String,dynamic>> validateDiscount(String code) =>
      _post("/discount/validate", {"code": code});

  // ── Ratings ──
  static Future<Map<String,dynamic>> rateStore(String token, String storeId, double rating) =>
      _post("/stores/$storeId/rate", {"rating": rating}, token: token);
  static Future<Map<String,dynamic>?> getUserRating(String token, String storeId) async {
    try { return Map<String,dynamic>.from(await _get("/stores/$storeId/my-rating", token: token)); } catch(_) { return null; }
  }

  // ── Favorites ──
  static Future<void> toggleFavorite(String token, String storeId) async {
    try { await _post("/user/favorites/$storeId", {}, token: token); } catch(_) {}
  }
  static Future<List> getFavorites(String token) async {
    try { return await _get("/user/favorites", token: token); } catch(_) { return []; }
  }
  static Future<bool> isFavorite(String token, String storeId) async {
    try { final d = await _get("/user/favorites/$storeId/check", token: token); return d["is_favorite"]==true; } catch(_) { return false; }
  }
}

// ─────────────────────── PREFS ───────────────────────
class Prefs {
  static Future<void> save(String token, String name, String phone, String role, {String city=""}) async {
    final p = await SharedPreferences.getInstance();
    await p.setString("token", token); await p.setString("name", name);
    await p.setString("phone", phone); await p.setString("role", role);
    if (city.isNotEmpty) await p.setString("city", city);
  }
  static Future<void> saveCity(String c) async { final p = await SharedPreferences.getInstance(); await p.setString("city", c); }
  static Future<Map<String,String?>> get() async {
    final p = await SharedPreferences.getInstance();
    return {"token": p.getString("token"), "name": p.getString("name"), "phone": p.getString("phone"), "role": p.getString("role"), "city": p.getString("city")};
  }
  static Future<void> clear() async { final p = await SharedPreferences.getInstance(); await p.clear(); }
}

// ─────────────────────── LOCATION ───────────────────────
// Haversine distance in km between two lat/lng points
double _haversineKm(double lat1, double lon1, double lat2, double lon2) {
  const r = 6371.0;
  final dLat = (lat2-lat1)*pi/180; final dLon = (lon2-lon1)*pi/180;
  final a = sin(dLat/2)*sin(dLat/2)+cos(lat1*pi/180)*cos(lat2*pi/180)*sin(dLon/2)*sin(dLon/2);
  return r*2*atan2(sqrt(a),sqrt(1-a));
}

Future<String> detectCity() async {
  try {
    var perm = await Geolocator.checkPermission();
    if (perm == LocationPermission.denied) perm = await Geolocator.requestPermission();
    if (perm == LocationPermission.deniedForever) return "Ballari";
    final pos = await Geolocator.getCurrentPosition(desiredAccuracy: LocationAccuracy.medium).timeout(const Duration(seconds: 10));
    final marks = await placemarkFromCoordinates(pos.latitude, pos.longitude);
    return marks.first.locality ?? marks.first.subAdministrativeArea ?? "Ballari";
  } catch (_) { return "Ballari"; }
}

// ─────────────────────── LOGO ───────────────────────
// ─────────────────────── OFFRO LOGO WIDGET ───────────────────────
// Renders the Offro pin+% icon. [color] tints the pin, [size] = pin height.
class OffroPinPainter extends CustomPainter {
  final Color color;
  OffroPinPainter(this.color);
  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width; final h = size.height;
    final pinH = h * .82; // pin occupies top 82%

    // ── Outer glow / shadow ring ──
    final glowPaint = Paint()..color = color.withOpacity(.18)..style = PaintingStyle.fill;
    final pinPath = _pinPath(w, pinH, w);
    canvas.drawPath(pinPath, glowPaint..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6));

    // ── Pin body filled ──
    final fillPaint = Paint()..color = color..style = PaintingStyle.fill;
    canvas.drawPath(_pinPath(w*.86, pinH*.9, w*.86, dx: w*.07, dy: 0), fillPaint);

    // ── Inner circle (light) ──
    final cx = w*.5; final cy = pinH*.38;
    final innerR = w*.22;
    canvas.drawCircle(Offset(cx, cy), innerR, Paint()..color = Colors.white.withOpacity(.55)..style = PaintingStyle.fill);

    // ── % symbol ──
    final tp = TextPainter(
      text: TextSpan(text: "%", style: TextStyle(color: Colors.white, fontSize: innerR*1.15, fontWeight: FontWeight.w900, height: 1.0)),
      textDirection: TextDirection.ltr,
    )..layout();
    tp.paint(canvas, Offset(cx - tp.width/2, cy - tp.height/2));

    // ── Signal dots above pin (like the logo) ──
    final dotPaint = Paint()..color = color.withOpacity(.35)..style = PaintingStyle.stroke..strokeWidth = w*.04..strokeCap = StrokeCap.round;
    for (int i = 1; i <= 2; i++) {
      final r = w*(.28 + i*.13);
      canvas.drawArc(Rect.fromCircle(center: Offset(cx, cy), radius: r), -2.4, 1.1*2, false, dotPaint..color = color.withOpacity(.3 - i*.08));
    }
  }

  Path _pinPath(double w, double h, double totalW, {double dx=0, double dy=0}) {
    final cx = totalW/2 + dx; final r = w/2;
    return Path()
      ..moveTo(cx, h + dy)
      ..cubicTo(cx - r*.25, h*.82 + dy, cx - r, h*.6 + dy, cx - r, h*.38 + dy)
      ..arcTo(Rect.fromCircle(center: Offset(cx, h*.38 + dy), radius: r), 3.14, -3.14, false)
      ..cubicTo(cx + r, h*.6 + dy, cx + r*.25, h*.82 + dy, cx, h + dy)
      ..close();
  }

  @override bool shouldRepaint(OffroPinPainter o) => o.color != color;
}

// Main logo widget — shows pin icon only (used in AppBar, empty states etc.)
Widget buildLogo(double size, Color color, {double progress=1.0}) =>
    CustomPaint(size: Size(size, size*1.25), painter: OffroPinPainter(color));

// Full brand logo: pin + "Offro" text + optional tagline
Widget buildBrandLogo({double pinSize=80, Color pinColor=kLight, Color textColor=Colors.white, bool showTagline=false}) =>
    Column(mainAxisSize: MainAxisSize.min, children: [
      CustomPaint(size: Size(pinSize, pinSize*1.25), painter: OffroPinPainter(pinColor)),
      const SizedBox(height: 10),
      RichText(text: TextSpan(children: [
        TextSpan(text: "Offr", style: TextStyle(color: textColor, fontSize: pinSize*.42, fontWeight: FontWeight.w900, letterSpacing: .5)),
        TextSpan(text: "O", style: TextStyle(color: pinColor, fontSize: pinSize*.42, fontWeight: FontWeight.w900)),
      ])),
      if (showTagline) ...[
        const SizedBox(height: 4),
        Text("Smart Savings Nearby", style: TextStyle(color: textColor.withOpacity(.65), fontSize: pinSize*.13, letterSpacing: .4)),
      ],
    ]);

// Image-based logo widget — uses actual PNG asset
Widget buildImageLogo({double height=60, bool white=false}) {
  final path = white ? 'assets/logo_white.png' : 'assets/logo_green.png';
  return Image.asset(
    path,
    height: height,
    fit: BoxFit.contain,
    errorBuilder: (_, __, ___) {
      // Fallback to text logo if asset missing
      return SizedBox(
        height: height,
        child: Center(
          child: RichText(text: TextSpan(children: [
            TextSpan(text:"Offr", style:TextStyle(color: white?Colors.white:const Color(0xFF1B4332), fontWeight:FontWeight.w900, fontSize:height*0.36, letterSpacing:0.5)),
            TextSpan(text:"O",    style:TextStyle(color: white?const Color(0xFFA9CDBA):const Color(0xFF2D6A4F), fontWeight:FontWeight.w900, fontSize:height*0.36)),
          ])),
        ),
      );
    },
  );
}

// ─────────────────────── MAIN ───────────────────────
Future<void> main() async {
  // Wrap the whole app in a guarded zone so any uncaught async Dart error
  // (Futures, timers, isolate callbacks, etc.) is forwarded to Crashlytics.
  runZonedGuarded<Future<void>>(() async {
    WidgetsFlutterBinding.ensureInitialized();

    // Initialize Firebase. Wrapped in try/catch so a config issue (missing
    // google-services.json, wrong package, etc.) cannot block the app from
    // starting — we still call runApp() below.
    try {
      await Firebase.initializeApp(
        options: DefaultFirebaseOptions.currentPlatform,
      );
      await FirebaseCrashlytics.instance
          .setCrashlyticsCollectionEnabled(!kDebugMode);

      // Framework (widget tree) errors → Crashlytics as fatal
      FlutterError.onError = (FlutterErrorDetails details) {
        FlutterError.presentError(details);
        FirebaseCrashlytics.instance.recordFlutterFatalError(details);
      };

      // Uncaught native/platform Dart errors → Crashlytics as fatal
      PlatformDispatcher.instance.onError = (Object error, StackTrace stack) {
        FirebaseCrashlytics.instance.recordError(error, stack, fatal: true);
        return true;
      };
    } catch (e, st) {
      // Firebase init failed (offline first launch, mis-configured google-services.json, etc.)
      // Keep going — the user can still use the app, and we log to console.
      // ignore: avoid_print
      print('Firebase init failed: $e\n$st');
    }

    SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      statusBarIconBrightness: Brightness.light,
    ));

    runApp(const MyApp());
  }, (Object error, StackTrace stack) {
    try {
      FirebaseCrashlytics.instance.recordError(error, stack, fatal: true);
    } catch (_) {/* Crashlytics unavailable — ignore */}
  });
}

IconData _categoryIcon(String cat) {
  switch(cat.toLowerCase()) {
    case "all": return Icons.apps_rounded;
    case "grocery": return Icons.shopping_basket_rounded;
    case "restaurant": return Icons.restaurant_rounded;
    case "pharmacy": return Icons.local_pharmacy_rounded;
    case "electronics": return Icons.devices_rounded;
    case "clothing": return Icons.checkroom_rounded;
    case "bakery": return Icons.cake_rounded;
    case "salon": return Icons.content_cut_rounded;
    case "pet store": return Icons.pets_rounded;
    default: return Icons.store_rounded;
  }
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});
  @override Widget build(BuildContext context) => MaterialApp(
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      primaryColor: kPrimary,
      colorScheme: ColorScheme.fromSeed(seedColor: kPrimary),
      useMaterial3: false,
      appBarTheme: const AppBarTheme(
        backgroundColor: kPrimary,
        foregroundColor: Colors.white,
        elevation: 0,
        systemOverlayStyle: SystemUiOverlayStyle(
          statusBarColor: Colors.transparent,
          statusBarIconBrightness: Brightness.light,
        ),
      ),
    ),
    home: const SplashScreen(),
  );
}

class _VoucherStyle {
  final Color bg, accent, iconBg, iconColor, codeColor, codeBg;
  const _VoucherStyle({required this.bg,required this.accent,required this.iconBg,required this.iconColor,required this.codeColor,required this.codeBg});
}

// ─────────────────────── SPLASH ───────────────────────

// ─────────────────────── NAV BTN (label on active only) ───────────────────────
class _NavBtn extends StatelessWidget {
  final IconData icon; final String label; final bool active;
  const _NavBtn({required this.icon,required this.label,required this.active});
  @override Widget build(BuildContext context) => AnimatedContainer(
    duration: const Duration(milliseconds:200),
    padding: const EdgeInsets.symmetric(horizontal:14, vertical:6),
    decoration: BoxDecoration(
      color: active ? const Color(0xFFF5EFE6) : Colors.transparent,
      borderRadius: BorderRadius.circular(20),
      boxShadow: active ? [
        BoxShadow(color:const Color(0xFFE7D7C8).withOpacity(.9), blurRadius:12, spreadRadius:1, offset:const Offset(0,2)),
        BoxShadow(color:const Color(0xFFCDEBD6).withOpacity(.4), blurRadius:6, offset:const Offset(0,1)),
      ] : [],
    ),
    child: Column(mainAxisAlignment:MainAxisAlignment.center,mainAxisSize:MainAxisSize.min,children:[
      Icon(icon, color:active?kPrimary:kMuted, size:24),
      AnimatedCrossFade(
        duration:const Duration(milliseconds:180),
        crossFadeState: active ? CrossFadeState.showFirst : CrossFadeState.showSecond,
        firstChild:Padding(padding:const EdgeInsets.only(top:2),
          child:Text(label,style:TextStyle(color:active?kPrimary:kMuted,fontSize:10,fontWeight:FontWeight.w700))),
        secondChild:const SizedBox(height:14),
      ),
    ]),
  );
}

// ─────────────────────── NIT STACK CAROUSEL ───────────────────────
class _NitStackCarousel extends StatefulWidget {
  final List<Map<String,dynamic>> stores;
  final String token;
  const _NitStackCarousel({required this.stores, required this.token});
  @override State<_NitStackCarousel> createState()=>_NitStackCarouselState();
}
class _NitStackCarouselState extends State<_NitStackCarousel> with TickerProviderStateMixin {
  int _top = 0;
  double _dragX = 0;
  bool _isDragging = false;
  late AnimationController _dismissAnim;
  late AnimationController _returnAnim;
  Timer? _autoSwipe;

  @override void initState(){
    super.initState();
    _top = 0;
    _dismissAnim = AnimationController(vsync:this, duration:const Duration(milliseconds:320));
    _returnAnim  = AnimationController(vsync:this, duration:const Duration(milliseconds:250));
    _dismissAnim.addStatusListener((s){
      if (s==AnimationStatus.completed){
        if (mounted) setState((){
          _top=(_top+1)%widget.stores.length;
          _dragX=0; _isDragging=false;
          _dismissAnim.reset(); _returnAnim.reset();
        });
      }
    });
    // Auto-slide every 4 seconds
    if(widget.stores.length > 1){
      _autoSwipe = Timer.periodic(const Duration(seconds:4), (_){
        if(mounted && !_isDragging && _dismissAnim.status==AnimationStatus.dismissed){
          _swipeLeft();
        }
      });
    }
  }
  @override void dispose(){ _autoSwipe?.cancel(); _dismissAnim.dispose(); _returnAnim.dispose(); super.dispose(); }

  void _swipeLeft(){
    _dismissAnim.forward();
  }

  @override Widget build(BuildContext context){
    if (widget.stores.isEmpty) return const SizedBox.shrink();
    final size=MediaQuery.of(context).size;
    final count=widget.stores.length;
    final cardH=size.height*0.50;
    final cardW=size.width-40.0;

    return Column(mainAxisSize:MainAxisSize.min, children:[
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: SizedBox(
        height: cardH+20,
        child: Stack(
          clipBehavior: Clip.hardEdge,
          alignment:Alignment.topCenter,
          children:[
            // ── Back cards: FIXED scales (0.92 / 0.84). No per-frame recompute. ──
            //    Using static Transform.scale eliminates the visible morphing
            //    that the user reported. The split-second size-snap when a back
            //    card becomes the top is masked by the front card sliding away
            //    in the same frame.
            for(int offset=min(count-1,2); offset>=1; offset--)
              Positioned(
                key: ValueKey('card_${(_top+offset)%count}'),
                top: 0,
                left: 0,
                right: 0,
                child: IgnorePointer(
                  child: Opacity(
                    opacity: offset == 1 ? 0.85 : 0.55,
                    child: Transform.scale(
                      scale: offset == 1 ? 0.92 : 0.84,
                      alignment: Alignment.topCenter,
                      child: SizedBox(
                        height: cardH,
                        child: Container(
                          margin: EdgeInsets.symmetric(horizontal: 2.0 * offset),
                          child: ClipRRect(
                            borderRadius: BorderRadius.circular(24),
                            child: RepaintBoundary(child: _BigCarouselCard(
                              key: ValueKey('cardImg_${(_top+offset)%count}'),
                              store: widget.stores[(_top+offset)%count],
                            )),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            // ── Top card: AnimatedBuilder only wraps the Transform, child is stable ──
            Positioned(
              key: ValueKey('card_$_top'),
              top:0, left:2, right:2,
              child: GestureDetector(
                onHorizontalDragStart:(_){ setState(()=>_isDragging=true); },
                onHorizontalDragUpdate:(d){ setState(()=>_dragX+=d.delta.dx); },
                onHorizontalDragEnd:(d){
                  final vel = d.velocity.pixelsPerSecond.dx;
                  if (_dragX < -cardW*0.30 || vel < -600){
                    _isDragging=false; _swipeLeft();
                  } else {
                    setState(()=>_isDragging=false);
                    _dragX=0;
                  }
                },
                onTap:()=>Navigator.push(context,_route(DetailPage(store:widget.stores[_top],token:widget.token))),
                child: AnimatedBuilder(
                  animation: Listenable.merge([_dismissAnim,_returnAnim]),
                  child: SizedBox(
                    height: cardH,
                    child: Container(
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(24),
                        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.2),blurRadius:18,offset:const Offset(0,6))],
                      ),
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(24),
                        child: RepaintBoundary(child: _BigCarouselCard(
                          key: ValueKey('cardImg_$_top'),
                          store: widget.stores[_top],
                        )),
                      ),
                    ),
                  ),
                  builder:(_, child){
                    final dismissOffset = _dismissAnim.value * -(size.width*1.2);
                    final curDragX = _isDragging ? _dragX : 0.0;
                    final totalX   = curDragX + dismissOffset;
                    final angle    = (totalX/size.width)*0.18;
                    return Transform(
                      transform: Matrix4.identity()
                        ..translate(totalX, 0.0)
                        ..rotateZ(angle),
                      alignment: FractionalOffset.bottomCenter,
                      child: child,
                    );
                  },
                ),
              ),
            ),
            // ── Swipe hint ──
            if(count>1 && !_isDragging)
              Positioned(
                right:20, top:cardH/2-16,
                child:IgnorePointer(child:Container(
                  padding:const EdgeInsets.all(8),
                  decoration:const BoxDecoration(color:Colors.black38,shape:BoxShape.circle),
                  child:const Icon(Icons.keyboard_arrow_left_rounded,color:Colors.white,size:22),
                )),
              ),
          ],
        ),
      ),
      ), // end Padding(horizontal:16)
      // ── Dots — internal, no parent setState ──
      if(count>1) Padding(
        padding:const EdgeInsets.only(top:8,bottom:4),
        child:Row(mainAxisAlignment:MainAxisAlignment.center,
          children:List.generate(count,(i){
            final active=i==_top;
            return AnimatedContainer(
              duration:const Duration(milliseconds:250),
              margin:const EdgeInsets.symmetric(horizontal:3),
              width:active?20.0:6.0, height:6,
              decoration:BoxDecoration(
                color:active?kPrimary:kBorder,
                borderRadius:BorderRadius.circular(3),
              ),
            );
          }),
        ),
      ),
    ]);
  }
}

// ─────────────────────── MASONRY SEARCH GRID ───────────────────────
class _MasonrySearchGrid extends StatelessWidget {
  final List<Map<String,dynamic>> stores; final String token;
  const _MasonrySearchGrid({required this.stores,required this.token});

  Widget _imgWidget(Map s) {
    final img = s["image"]?.toString() ?? "";
    if (img.startsWith("data:image")) {
      try { return Image.memory(base64Decode(img.split(",").last),fit:BoxFit.cover,width:double.infinity,height:double.infinity,gaplessPlayback:true); }
      catch(_) {}
    }
    return Container(color:kAccent, child:Center(child:Icon(Icons.store,color:kPrimary,size:32)));
  }

  @override Widget build(BuildContext context) {
    final rng = Random(42);
    // Generate random heights: alternate between tall/medium/short
    final heights = List.generate(stores.length, (i) => [140.0,180.0,120.0,160.0,200.0,130.0][i%6]);
    // Build 2-column masonry
    final col1 = <int>[]; final col2 = <int>[];
    double h1=0, h2=0;
    for(int i=0;i<stores.length;i++){
      if(h1<=h2){col1.add(i);h1+=heights[i]+10;}
      else{col2.add(i);h2+=heights[i]+10;}
    }
    Widget _card(int idx) {
      final s=Map<String,dynamic>.from(stores[idx] as Map);
      final dist=(s["distance_km"] as num?)?.toDouble();
      final rating=(s["rating"] as num?)?.toDouble()??0;
      return GestureDetector(
        onTap:()=>Navigator.push(context,_route(DetailPage(store:s,token:token))),
        child:Container(
          height:heights[idx],
          margin:const EdgeInsets.only(bottom:10),
          decoration:BoxDecoration(
            borderRadius:BorderRadius.circular(16),
            boxShadow:[BoxShadow(color:Colors.black.withOpacity(.1),blurRadius:8,offset:const Offset(0,3))],
          ),
          child:ClipRRect(
            borderRadius:BorderRadius.circular(16),
            child:Stack(fit:StackFit.expand,children:[
              _imgWidget(s),
              Positioned.fill(child:DecoratedBox(decoration:BoxDecoration(
                gradient:LinearGradient(begin:Alignment.topCenter,end:Alignment.bottomCenter,
                  colors:[Colors.transparent,Colors.transparent,Colors.black.withOpacity(.7)],stops:const[0,.5,1]),
              ))),
              if(dist!=null) Positioned(top:7,right:7,child:Container(
                padding:const EdgeInsets.symmetric(horizontal:6,vertical:3),
                decoration:BoxDecoration(color:Colors.black54,borderRadius:BorderRadius.circular(8)),
                child:Text(dist<1?"${(dist*1000).round()}m":"${dist.toStringAsFixed(1)}km",
                  style:const TextStyle(color:Colors.white,fontSize:9,fontWeight:FontWeight.w700)),
              )),
              Positioned(bottom:0,left:0,right:0,child:Padding(
                padding:const EdgeInsets.fromLTRB(8,0,8,8),
                child:Column(crossAxisAlignment:CrossAxisAlignment.start,mainAxisSize:MainAxisSize.min,children:[
                  Text(s["store_name"]?.toString()??"",style:const TextStyle(color:Colors.white,fontSize:12,fontWeight:FontWeight.w800),maxLines:1,overflow:TextOverflow.ellipsis),
                  if(rating>0) Row(children:[
                    const Icon(Icons.star_rounded,color:Color(0xFFFFD700),size:10),const SizedBox(width:2),
                    Text(rating.toStringAsFixed(1),style:const TextStyle(color:Colors.white,fontSize:9,fontWeight:FontWeight.w700)),
                  ]),
                ]),
              )),
            ]),
          ),
        ),
      );
    }
    return SingleChildScrollView(
      padding:const EdgeInsets.fromLTRB(12,12,12,24),
      child:Row(crossAxisAlignment:CrossAxisAlignment.start,children:[
        Expanded(child:Column(children:col1.map(_card).toList())),
        const SizedBox(width:10),
        Expanded(child:Column(children:col2.map(_card).toList())),
      ]),
    );
  }
}

// ─────────────────────── GIFT VOUCHER CARD WIDGET ───────────────────────
class _GiftVoucherCard extends StatelessWidget {
  final Map voucher;
  const _GiftVoucherCard({required this.voucher});

  // Use store image2 (from linked store) as banner; fallback to gift icon
  Widget _bannerWidget() {
    // If there is a nested "store" object, pull image2 from it first
    final storeObj = voucher["store"];
    if (storeObj is Map) {
      for (final k in ["image2","image","img","photo"]) {
        final si = storeObj[k]?.toString() ?? "";
        if (si.isEmpty) continue;
        if (si.startsWith("data:image")) {
          try { return Image.memory(base64Decode(si.split(",").last),
            fit:BoxFit.cover, width:90, height:double.infinity, gaplessPlayback:true); } catch(_) {}
        }
        if (si.startsWith("http")) {
          return CachedNetworkImage(imageUrl:si, fit:BoxFit.cover, width:90, height:double.infinity,
            placeholder:(_,__)=>Container(width:90,color:const Color(0xFFEDD5A0),child:const Center(child:CircularProgressIndicator(color:Color(0xFFB8860B),strokeWidth:2))),
            errorWidget:(_,__,___)=>Container(width:90,color:const Color(0xFFEDD5A0),child:const Center(child:Icon(Icons.card_giftcard,color:Color(0xFFB8860B),size:32))));
        }
      }
    }
    // Try all possible image field names — image2 is the 2nd store photo
    for (final key in ["logo","image2","store_image2","img2","photo2","banner","store_banner","store_image","store_img","image","photo","img"]) {
      final img = voucher[key]?.toString() ?? "";
      if (img.isEmpty) continue;
      if (img.startsWith("data:image")) {
        try {
          return Image.memory(base64Decode(img.split(",").last),
            fit:BoxFit.cover, width:90, height:double.infinity, gaplessPlayback:true);
        } catch(_) { continue; }
      }
      if (img.startsWith("http") || img.startsWith("https")) {
        return CachedNetworkImage(imageUrl:img, fit:BoxFit.cover, width:90, height:double.infinity,
          placeholder:(_,__)=>Container(width:90,color:const Color(0xFFEDD5A0),child:const Center(child:CircularProgressIndicator(color:Color(0xFFB8860B),strokeWidth:2))),
          errorWidget:(_,__,___)=>Container(width:90,color:const Color(0xFFEDD5A0),child:const Center(child:Icon(Icons.card_giftcard,color:Color(0xFFB8860B),size:32))));
      }
    }
    return Container(width:90, color:const Color(0xFFEDD5A0),
      child:const Center(child:Icon(Icons.card_giftcard,color:Color(0xFFB8860B),size:32)));
  }

  @override Widget build(BuildContext context) {
    final title   = voucher["title"]?.toString() ?? "";
    final text    = voucher["text"]?.toString() ?? "";
    final price   = voucher["price"]?.toString() ?? voucher["value"]?.toString() ?? "";
    final validity= voucher["validity"]?.toString() ?? "";
    final bgColor1 = const Color(0xFFF5E6C8);
    final bgColor2 = const Color(0xFFEDD5A0);
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.symmetric(horizontal:12, vertical:6),
      height: 128,
      decoration: BoxDecoration(
        gradient: LinearGradient(colors:[bgColor1,bgColor2],begin:Alignment.topLeft,end:Alignment.bottomRight),
        borderRadius: BorderRadius.circular(16),
        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.08),blurRadius:10,offset:const Offset(0,4))],
        border: Border.all(color:const Color(0xFFD4A843).withOpacity(.6),width:1),
      ),
      child: Row(children:[
        // Left image section - store's image2
        ClipRRect(
          borderRadius: const BorderRadius.horizontal(left:Radius.circular(16)),
          child: Stack(children:[
            SizedBox(width:90,height:double.infinity,child:_bannerWidget()),
            // Notch top
            Positioned(top:-12,right:-12,child:Container(width:24,height:24,decoration:const BoxDecoration(color:Color(0xFFFDFBF6),shape:BoxShape.circle))),
            // Notch bottom
            Positioned(bottom:-12,right:-12,child:Container(width:24,height:24,decoration:const BoxDecoration(color:Color(0xFFFDFBF6),shape:BoxShape.circle))),
            // (center overlay removed — store image is the full left panel background)
          ]),
        ),
        // Right content section
        Expanded(child:Padding(
          padding:const EdgeInsets.fromLTRB(14,10,12,10),
          child:Column(crossAxisAlignment:CrossAxisAlignment.center,mainAxisAlignment:MainAxisAlignment.center,children:[
            if(price.isNotEmpty) Text("₹$price",style:const TextStyle(color:Color(0xFF3A2A00),fontSize:22,fontWeight:FontWeight.w900,height:1.1)),
            if(title.isNotEmpty) Container(
              margin:const EdgeInsets.only(top:4),
              padding:const EdgeInsets.symmetric(horizontal:8,vertical:2),
              decoration:BoxDecoration(color:const Color(0xFFB8860B).withOpacity(.15),borderRadius:BorderRadius.circular(6)),
              child:Text(title,style:const TextStyle(color:Color(0xFFB8860B),fontSize:10,fontWeight:FontWeight.w800,letterSpacing:0.5)),
            ),
            const SizedBox(height:4),
            Text(text,style:const TextStyle(color:Color(0xFF3A2A00),fontSize:13,fontWeight:FontWeight.w700,height:1.2),maxLines:2,overflow:TextOverflow.ellipsis,textAlign:TextAlign.center),
            if(validity.isNotEmpty)...[
              const SizedBox(height:4),
              Text("Valid: $validity",style:const TextStyle(color:Color(0xFF7A5C1A),fontSize:10,fontWeight:FontWeight.w500)),
            ],
          ]),
        )),
        // Arrow
        Padding(padding:const EdgeInsets.only(right:14),
          child:const Icon(Icons.arrow_forward_ios_rounded,color:Color(0xFF7A5C1A),size:14)),
      ]),
    );
  }
}

class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});
  @override State<SplashScreen> createState() => _SplashState();
}
class _SplashState extends State<SplashScreen> with SingleTickerProviderStateMixin {
  late AnimationController _c;
  late Animation<double> _draw, _fade;
  @override void initState() {
    super.initState();
    _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 2000));
    _draw = CurvedAnimation(parent: _c, curve: const Interval(0.0, 0.75, curve: Curves.easeInOut));
    _fade = CurvedAnimation(parent: _c, curve: const Interval(0.45, 1.0, curve: Curves.easeIn));
    _c.forward(); _checkLogin();
  }
  @override void dispose() { _c.dispose(); super.dispose(); }

  Future<void> _checkLogin() async {
    // Always end up on SOME screen, even if anything below throws. Without
    // this guard, an exception in Prefs/Api would be silently captured by
    // the runZonedGuarded handler in main() → splash would hang forever.
    try {
      // Splash duration — bumped from 1500ms so the brand mark sits a touch longer.
      await Future.delayed(const Duration(milliseconds: 2500));
      if (!mounted) return;
      final u = await Prefs.get();
      final token = u["token"]; final role = u["role"] ?? "user";
      if (token != null) {
        if (role == "merchant") {
          final me = await Api.getMerchantMe(token);
          if (me != null && mounted) { Navigator.pushReplacement(context, _route(MerchantHome(token:token, merchant:me))); return; }
        } else {
          final me = await Api.getMe(token);
          if (me != null && mounted) { Navigator.pushReplacement(context, _route(HomeScreen(token:token, name:me["name"]??"", phone:me["phone"]??"", savedCity:u["city"]??''))); return; }
        }
      }
    } catch (e, st) {
      // Forward to Crashlytics for visibility, but don't block navigation.
      try { FirebaseCrashlytics.instance.recordError(e, st, fatal: false, reason: 'splash _checkLogin failed'); } catch (_) {}
    }
    // Not logged in (or anything failed). Show onboarding before login —
    // per product spec: show onboarding whenever user is not logged in.
    if (mounted) Navigator.pushReplacement(context, _route(const OnboardingScreen()));
  }

  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor: Colors.white,
    body: AnimatedBuilder(animation: _c, builder: (_,__) =>
      Container(
        width: double.infinity, height: double.infinity,
        color: Colors.white,
        child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
          const Spacer(flex:3),
          // Logo with scale + fade animation
          FadeTransition(
            opacity: _fade,
            child: ScaleTransition(
              scale: Tween<double>(begin:0.75, end:1.0).animate(
                CurvedAnimation(parent:_c, curve: const Interval(0.0, 0.8, curve:Curves.easeOutBack))),
              child: buildImageLogo(height:110, white:false),
            ),
          ),
          const SizedBox(height:20),
          // Tagline fade in
          FadeTransition(
            opacity: Tween<double>(begin:0,end:1).animate(
              CurvedAnimation(parent:_c, curve: const Interval(0.5,1.0,curve:Curves.easeIn))),
            child: const Text("Discover · Save · Earn",
              style:TextStyle(color:kMuted, fontSize:13, letterSpacing:2.2, fontWeight:FontWeight.w500)),
          ),
          const Spacer(flex:3),
          // Loader at bottom
          FadeTransition(
            opacity: _fade,
            child: Padding(
              padding: const EdgeInsets.only(bottom:48),
              child: SizedBox(width:22, height:22,
                child: CircularProgressIndicator(color:kPrimary, strokeWidth:2.5)),
            ),
          ),
        ]),
      ),
    ),
  );
}

PageRoute _route(Widget w) => MaterialPageRoute(builder: (_) => w);

// ─────────────────────── OTP SCREEN ───────────────────────
class OtpScreen extends StatefulWidget {
  final String phone;
  final String role;
  final Future<void> Function() onVerified;
  const OtpScreen({super.key, required this.phone, required this.role, required this.onVerified});
  @override State<OtpScreen> createState() => _OtpScreenState();
}

class _OtpScreenState extends State<OtpScreen> {
  final List<TextEditingController> _ctls = List.generate(4, (_) => TextEditingController());
  final List<FocusNode> _foci = List.generate(4, (_) => FocusNode());
  bool _loading = false;
  String _msg = "";
  int _resendSecs = 30;
  Timer? _resendTimer;
  static const String _devOtp = "1234"; // hardcoded until real OTP integrated

  @override void initState() {
    super.initState();
    _startResendTimer();
    // Focus first field
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) FocusScope.of(context).requestFocus(_foci[0]);
    });
  }

  @override void dispose() {
    for (var c in _ctls) c.dispose();
    for (var f in _foci) f.dispose();
    _resendTimer?.cancel();
    super.dispose();
  }

  void _startResendTimer() {
    _resendTimer?.cancel();
    setState(() => _resendSecs = 30);
    _resendTimer = Timer.periodic(const Duration(seconds:1), (t) {
      if (!mounted) { t.cancel(); return; }
      if (_resendSecs <= 0) { t.cancel(); setState(()=>_resendSecs=0); return; }
      setState(() => _resendSecs--);
    });
  }

  String get _enteredOtp => _ctls.map((c) => c.text).join();

  Future<void> _verify() async {
    final otp = _enteredOtp;
    if (otp.length < 4) { setState(() => _msg = "Enter 4-digit OTP"); return; }
    if (otp != _devOtp) { setState(() => _msg = "Incorrect OTP. Try 1234"); return; }
    setState(() { _loading = true; _msg = ""; });
    try {
      await widget.onVerified();
    } catch(e) {
      if (mounted) setState(() { _msg = e.toString().replaceAll("Exception: ",""); _loading = false; });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Stack(children: [
        // bg gradient
        Container(decoration: const BoxDecoration(gradient: LinearGradient(
          colors: [Color(0xFF0d2b24), Color(0xFF1e4a3f), Color(0xFF3E5F55)],
          begin: Alignment.topLeft, end: Alignment.bottomRight))),

        SafeArea(child: Column(children: [
          // back button
          Align(
            alignment: Alignment.centerLeft,
            child: IconButton(
              icon: const Icon(Icons.arrow_back_ios_new_rounded, color:Colors.white),
              onPressed: () => Navigator.pop(context),
            ),
          ),

          Expanded(child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal:28, vertical:12),
            child: Column(crossAxisAlignment:CrossAxisAlignment.center, children:[
              const SizedBox(height:20),

              // Logo
              buildImageLogo(height:65, white:true),
              const SizedBox(height:32),

              // Icon
              Container(
                width:72, height:72,
                decoration: BoxDecoration(
                  color: kLight.withOpacity(.15),
                  shape: BoxShape.circle,
                  border: Border.all(color:kLight.withOpacity(.3), width:2),
                ),
                child: const Icon(Icons.lock_outline_rounded, color:kLight, size:34),
              ),
              const SizedBox(height:20),

              // Title
              const Text("Verify Your Number",
                style:TextStyle(color:Colors.white, fontSize:22, fontWeight:FontWeight.w900)),
              const SizedBox(height:8),
              Text("We sent a 4-digit OTP to\n${widget.phone}",
                textAlign:TextAlign.center,
                style:TextStyle(color:Colors.white.withOpacity(.65), fontSize:13, height:1.5)),
              Container(
                margin:const EdgeInsets.only(top:6),
                padding:const EdgeInsets.symmetric(horizontal:12,vertical:5),
                decoration:BoxDecoration(
                  color:Colors.amber.withOpacity(.15),
                  borderRadius:BorderRadius.circular(8),
                  border:Border.all(color:Colors.amber.withOpacity(.4)),
                ),
                child:const Text("Demo mode: use OTP  1 2 3 4",
                  style:TextStyle(color:Colors.amber, fontSize:11, fontWeight:FontWeight.w600)),
              ),

              const SizedBox(height:32),

              // OTP boxes
              Row(
                mainAxisAlignment:MainAxisAlignment.center,
                children:List.generate(4,(i)=>Container(
                  width:60, height:64,
                  margin:const EdgeInsets.symmetric(horizontal:6),
                  decoration:BoxDecoration(
                    color:Colors.white.withOpacity(.1),
                    borderRadius:BorderRadius.circular(16),
                    border:Border.all(
                      color:_ctls[i].text.isNotEmpty ? kLight : Colors.white.withOpacity(.25),
                      width:_ctls[i].text.isNotEmpty ? 2 : 1.5),
                  ),
                  child:TextField(
                    controller:_ctls[i],
                    focusNode:_foci[i],
                    textAlign:TextAlign.center,
                    keyboardType:TextInputType.number,
                    maxLength:1,
                    style:const TextStyle(color:Colors.white, fontSize:24, fontWeight:FontWeight.w900),
                    decoration:const InputDecoration(
                      counterText:"",
                      border:InputBorder.none,
                    ),
                    onChanged:(v){
                      setState((){});
                      if(v.isNotEmpty && i < 3){
                        FocusScope.of(context).requestFocus(_foci[i+1]);
                      } else if(v.isEmpty && i > 0){
                        FocusScope.of(context).requestFocus(_foci[i-1]);
                      }
                      if(_enteredOtp.length==4) _verify();
                    },
                  ),
                )),
              ),

              if(_msg.isNotEmpty)...[
                const SizedBox(height:14),
                Text(_msg, style:const TextStyle(color:Color(0xFFFF6B6B), fontSize:13, fontWeight:FontWeight.w600)),
              ],

              const SizedBox(height:28),

              // Verify button
              SizedBox(
                width:double.infinity,
                child:ElevatedButton(
                  onPressed:_loading?null:_verify,
                  style:ElevatedButton.styleFrom(
                    backgroundColor:kLight,
                    foregroundColor:kPrimary,
                    padding:const EdgeInsets.symmetric(vertical:15),
                    shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(16)),
                    elevation:0,
                  ),
                  child:_loading
                    ? const SizedBox(width:22,height:22,child:CircularProgressIndicator(color:kPrimary,strokeWidth:2.5))
                    : const Text("Verify OTP", style:TextStyle(fontWeight:FontWeight.w900, fontSize:16)),
                ),
              ),

              const SizedBox(height:18),

              // Resend
              GestureDetector(
                onTap: _resendSecs == 0 ? () { _startResendTimer(); } : null,
                child: RichText(text:TextSpan(children:[
                  TextSpan(text:"Didn't receive OTP? ",
                    style:TextStyle(color:Colors.white.withOpacity(.6), fontSize:13)),
                  TextSpan(
                    text: _resendSecs > 0 ? "Resend in ${_resendSecs}s" : "Resend OTP",
                    style:TextStyle(
                      color: _resendSecs > 0 ? Colors.white38 : kLight,
                      fontWeight:FontWeight.w700, fontSize:13)),
                ])),
              ),

              const SizedBox(height:24),
            ]),
          )),
        ])),
      ]),
    );
  }
}

// ─────────────────────── ONBOARDING ───────────────────────
class OnboardingScreen extends StatefulWidget {
  const OnboardingScreen({super.key});
  @override State<OnboardingScreen> createState() => _OnboardingState();
}

class _OnboardingState extends State<OnboardingScreen> {
  final PageController _pc = PageController();
  int _idx = 0;

  static const List<String> _assets = [
    'assets/onboarding/screen1.webp',
    'assets/onboarding/screen2.webp',
    'assets/onboarding/screen3.webp',
  ];

  @override void dispose() { _pc.dispose(); super.dispose(); }

  void _advance() {
    if (_idx < _assets.length - 1) {
      _pc.nextPage(duration: const Duration(milliseconds: 320), curve: Curves.easeInOut);
    } else {
      _goToLogin();
    }
  }

  void _goToLogin() {
    if (!mounted) return;
    Navigator.pushReplacement(context, _route(const LoginScreen()));
  }

  @override Widget build(BuildContext context) => PopScope(
    // Allow system back to pop the route only when we're on the first page.
    // On pages 2 & 3 we intercept it and animate back to the previous page.
    canPop: _idx == 0,
    onPopInvokedWithResult: (didPop, _) {
      if (!didPop && _idx > 0) {
        _pc.previousPage(duration: const Duration(milliseconds: 320), curve: Curves.easeInOut);
      }
    },
    child: Scaffold(
      backgroundColor: Colors.white,
      body: SafeArea(
        child: Stack(children: [
          // ── The 3 designed screens (each baked-in title/illustration/button) ──
          PageView.builder(
            controller: _pc,
            itemCount: _assets.length,
            onPageChanged: (i) => setState(() => _idx = i),
            itemBuilder: (_, i) => Padding(
              // Leave room for the Offro logo on top
              padding: const EdgeInsets.only(top: 48),
              child: Image.asset(
                _assets[i],
                fit: BoxFit.contain,
                width: double.infinity,
                alignment: Alignment.topCenter,
              ),
            ),
          ),

          // ── Offro brand mark, top-center ──
          Positioned(
            top: 8, left: 0, right: 0,
            child: Center(child: buildImageLogo(height: 32, white: false)),
          ),

          // ── Skip button, top-right (hidden on last page) ──
          if (_idx < _assets.length - 1)
            Positioned(
              top: 4, right: 8,
              child: TextButton(
                onPressed: _goToLogin,
                style: TextButton.styleFrom(foregroundColor: kPrimary),
                child: const Text("Skip", style: TextStyle(fontWeight: FontWeight.w600)),
              ),
            ),

          // ── Tappable button overlay covering the baked-in CTA in each design.
          //    The button graphic ("I'm a Merchant" / "Explore Deals" /
          //    "Let's Get Started") is part of the image, so we just put an
          //    invisible tap target over that area and dispatch _advance().
          Positioned(
            left: 16, right: 16, bottom: 28,
            height: 64,
            child: GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTap: _advance,
              child: const SizedBox.expand(),
            ),
          ),
        ]),
      ),
    ),
  );
}

// ─────────────────────── LOGIN SCREEN ───────────────────────
class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override State<LoginScreen> createState() => _LoginState();
}

class _LoginState extends State<LoginScreen> with SingleTickerProviderStateMixin {
  late TabController _tab;
  late Future<Map<String,dynamic>> _socialFuture;
  // shared
  bool _loading = false; String _msg = ""; bool _msgOk = false;
  String _userCC = "+91"; String _merchCC = "+91";
  // user
  bool _isReg = false; bool _agreedUser = false;
  bool _agreedMerch = false;
  final _phoneC = TextEditingController(); final _nameC = TextEditingController();
  // merchant
  bool _isMerchReg = false;
  final _mPhoneC = TextEditingController(); final _mNameC = TextEditingController();
  final _mCityC  = TextEditingController(); final _mAreaC  = TextEditingController();

  @override void initState() { super.initState(); _tab = TabController(length: 2, vsync: this); _tab.addListener(() { if (!_tab.indexIsChanging) setState(() { _msg = ""; }); }); _socialFuture = Api.getSocialLinks(); }
  @override void dispose() { _tab.dispose(); _phoneC.dispose(); _nameC.dispose(); _mPhoneC.dispose(); _mNameC.dispose(); _mCityC.dispose(); _mAreaC.dispose(); super.dispose(); }

  void _setMsg(String m, {bool ok=false}) => setState(() { _msg=m; _msgOk=ok; });

  Future<void> _loginUser() async {
    if (_phoneC.text.trim().isEmpty) { _setMsg("Enter phone number"); return; }
    setState(()=>_loading=true); _msg="";
    try {
      // First check if user exists via login attempt
      final d = await Api.loginUser('$_userCC${_phoneC.text.trim()}');
      if (!mounted) return;
      setState(()=>_loading=false);
      // Navigate to OTP screen — pass the data to use after OTP
      Navigator.push(context, _route(OtpScreen(
        phone: '$_userCC${_phoneC.text.trim()}',
        role: "user",
        onVerified: () async {
          await Prefs.save(d["token"], d["name"]??"", d["phone"]??"", "user");
          if (!mounted) return;
          Navigator.pushReplacement(context, _route(HomeScreen(token:d["token"], name:d["name"]??"", phone:d["phone"]??"", savedCity:"")));
        },
      )));
    } catch(e) { _setMsg(e.toString().replaceAll("Exception: ","")); }
    if (mounted) setState(()=>_loading=false);
  }

  Future<void> _registerUser() async {
    if (_nameC.text.trim().isEmpty || _phoneC.text.trim().isEmpty) { _setMsg("Name and phone required"); return; }
    setState(()=>_loading=true); _msg="";
    try {
      await Api.registerUser(_nameC.text.trim(), '$_userCC${_phoneC.text.trim()}');
      if (!mounted) return;
      setState(() { _isReg=false; _phoneC.text=_phoneC.text.trim(); _nameC.clear(); });
      _setMsg("✅ Registered! Login now.", ok:true);
    } catch(e) { _setMsg(e.toString().replaceAll("Exception: ","")); }
    if (mounted) setState(()=>_loading=false);
  }

  Future<void> _loginMerchant() async {
    if (_mPhoneC.text.trim().isEmpty) { _setMsg("Enter phone number"); return; }
    setState(()=>_loading=true); _msg="";
    try {
      final d = await Api.loginMerchant('$_merchCC${_mPhoneC.text.trim()}');
      if (!mounted) return;
      setState(()=>_loading=false);
      Navigator.push(context, _route(OtpScreen(
        phone: '$_merchCC${_mPhoneC.text.trim()}',
        role: "merchant",
        onVerified: () async {
          await Prefs.save(d["token"], d["name"]??"", d["phone"]??"", "merchant");
          if (!mounted) return;
          final me = await Api.getMerchantMe(d["token"]) ?? {};
          Navigator.pushReplacement(context, _route(MerchantHome(token:d["token"], merchant:me)));
        },
      )));
    } catch(e) { _setMsg(e.toString().replaceAll("Exception: ","")); }
    if (mounted) setState(()=>_loading=false);
  }

  Future<void> _registerMerchant() async {
    if (_mNameC.text.trim().isEmpty || _mPhoneC.text.trim().isEmpty) { _setMsg("Name and phone required"); return; }
    setState(()=>_loading=true); _msg="";
    try {
      await Api.registerMerchant(_mNameC.text.trim(), '$_merchCC${_mPhoneC.text.trim()}', _mCityC.text.trim(), _mAreaC.text.trim());
      if (!mounted) return;
      setState(() { _isMerchReg=false; _mPhoneC.text=_mPhoneC.text.trim(); _mNameC.clear(); _mCityC.clear(); _mAreaC.clear(); });
      _setMsg("✅ Registered! Login now.", ok:true);
    } catch(e) { _setMsg(e.toString().replaceAll("Exception: ","")); }
    if (mounted) setState(()=>_loading=false);
  }

  @override
  Widget build(BuildContext context) {
    final h = MediaQuery.of(context).size.height;
    final w = MediaQuery.of(context).size.width;
    return Scaffold(
      resizeToAvoidBottomInset: true,
      backgroundColor: kBg,
      body: Stack(children: [

        // ── Top dark green curved section ──
        Positioned(top:0,left:0,right:0,
          child:Container(
            height: h * 0.40,
            decoration: const BoxDecoration(
              color: kPrimary,
              borderRadius: BorderRadius.vertical(bottom: Radius.circular(36)),
            ),
            child: SafeArea(
              child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
                const SizedBox(height:8),
                buildImageLogo(height:80, white:true),
                const SizedBox(height:12),
                const Text("Discover · Save · Earn",
                  style:TextStyle(color:Colors.white70, fontSize:12, letterSpacing:2.0, fontWeight:FontWeight.w500)),
              ]),
            ),
          ),
        ),

        // ── Decorative circles ──
        Positioned(top:-30, right:-30, child:Container(width:120,height:120,
          decoration:BoxDecoration(shape:BoxShape.circle,color:Colors.white.withOpacity(.05)))),
        Positioned(top: h*0.10, left:-20, child:Container(width:80,height:80,
          decoration:BoxDecoration(shape:BoxShape.circle,color:Colors.white.withOpacity(.04)))),

        // ── Scrollable content ──
        SingleChildScrollView(
          padding: EdgeInsets.zero,
          child: Column(children:[
            SizedBox(height: h * 0.34),

            // ── White card floats over green ──
            Container(
              margin: const EdgeInsets.symmetric(horizontal:20),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(28),
                boxShadow:[BoxShadow(color:Colors.black.withOpacity(.12),blurRadius:30,offset:const Offset(0,8))],
              ),
              child: Column(mainAxisSize:MainAxisSize.min, children:[

                // ── User / Merchant toggle ──
                Container(
                  margin: const EdgeInsets.fromLTRB(16,16,16,0),
                  decoration: BoxDecoration(
                    color: const Color(0xFFEFF7F2),
                    borderRadius: BorderRadius.circular(16),
                  ),
                  child: Row(children:[
                    Expanded(child:GestureDetector(
                      onTap:(){ _tab.animateTo(0); setState((){}); },
                      child:AnimatedContainer(
                        duration:const Duration(milliseconds:200),
                        padding:const EdgeInsets.symmetric(vertical:13),
                        decoration:BoxDecoration(
                          color: _tab.index==0 ? kPrimary : Colors.transparent,
                          borderRadius:BorderRadius.circular(14),
                        ),
                        child:Row(mainAxisAlignment:MainAxisAlignment.center,children:[
                          Icon(Icons.person_rounded, size:15,
                            color:_tab.index==0?Colors.white:kMuted),
                          const SizedBox(width:5),
                          Text("User", textAlign:TextAlign.center,
                            style:TextStyle(fontWeight:FontWeight.w700,fontSize:13,
                              color:_tab.index==0?Colors.white:kMuted)),
                        ]),
                      ),
                    )),
                    Expanded(child:GestureDetector(
                      onTap:(){ _tab.animateTo(1); setState((){}); },
                      child:AnimatedContainer(
                        duration:const Duration(milliseconds:200),
                        padding:const EdgeInsets.symmetric(vertical:13),
                        decoration:BoxDecoration(
                          color:_tab.index==1?kPrimary:Colors.transparent,
                          borderRadius:BorderRadius.circular(14),
                        ),
                        child:Row(mainAxisAlignment:MainAxisAlignment.center,children:[
                          Icon(Icons.store_rounded, size:15,
                            color:_tab.index==1?Colors.white:kMuted),
                          const SizedBox(width:5),
                          Text("Merchant", textAlign:TextAlign.center,
                            style:TextStyle(fontWeight:FontWeight.w700,fontSize:13,
                              color:_tab.index==1?Colors.white:kMuted)),
                        ]),
                      ),
                    )),
                  ]),
                ),

                // ── Tab content ──
                AnimatedSwitcher(
                  duration: const Duration(milliseconds:200),
                  child: KeyedSubtree(
                    key: ValueKey(_tab.index),
                    child: _tab.index==0 ? _userTab() : _merchantTab(),
                  ),
                ),
              ]),
            ),

            const SizedBox(height:32),
          ]),
        ),
      ]),
    );
  }

  Widget _userTab() => Padding(padding: const EdgeInsets.fromLTRB(22,4,22,22), child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
    if (_isReg) ...[_fld(_nameC, "Full Name", Icons.person_outline), const SizedBox(height: 10)],
    _ccPhoneRow(_userCC, (v)=>setState(()=>_userCC=v), _phoneC, "Phone Number"),
    const SizedBox(height: 14),
    if (_isReg) ...[
      Row(crossAxisAlignment:CrossAxisAlignment.start,children:[
        SizedBox(width:22,height:22,child:Checkbox(value:_agreedUser,onChanged:(v)=>setState(()=>_agreedUser=v??false),activeColor:kPrimary,materialTapTargetSize:MaterialTapTargetSize.shrinkWrap)),
        const SizedBox(width:6),
        Expanded(child:Wrap(children:[
          const Text("I agree to the ",style:TextStyle(fontSize:12,color:kMuted)),
          GestureDetector(onTap:()=>_showPolicy("Terms & Conditions",Api.fetchTerms("user")),child:const Text("Terms",style:TextStyle(fontSize:12,color:kPrimary,decoration:TextDecoration.underline,fontWeight:FontWeight.bold))),
          const Text(", ",style:TextStyle(fontSize:12,color:kMuted)),
          GestureDetector(onTap:()=>_showPolicy("Privacy Policy",Api.fetchPolicy("privacy")),child:const Text("Privacy Policy",style:TextStyle(fontSize:12,color:kPrimary,decoration:TextDecoration.underline,fontWeight:FontWeight.bold))),
          const Text(" & ",style:TextStyle(fontSize:12,color:kMuted)),
          GestureDetector(onTap:()=>_showPolicy("Refund Policy",Api.fetchPolicy("refund")),child:const Text("Refund Policy",style:TextStyle(fontSize:12,color:kPrimary,decoration:TextDecoration.underline,fontWeight:FontWeight.bold))),
        ])),
      ]),
      const SizedBox(height:10),
    ],
    _btn(_isReg ? "Create Account" : "Login", _loading ? null : (_isReg ? (_agreedUser ? _registerUser : null) : _loginUser)),
    if (_isReg && !_agreedUser) const Padding(padding:EdgeInsets.only(top:5),child:Text("Please accept terms to continue",textAlign:TextAlign.center,style:TextStyle(color:Color(0xFFb56a3a),fontSize:11))),
    if (_msg.isNotEmpty) ...[const SizedBox(height:10), _msgWidget()],
    const SizedBox(height: 14),
    Center(child: GestureDetector(
      onTap: () => setState(() { _isReg=!_isReg; _msg=""; _agreedUser=false; }),
      child: RichText(text: TextSpan(children: [
        TextSpan(text: _isReg ? "Have an account? " : "New here? ",
            style: const TextStyle(color: kMuted, fontSize: 13)),
        TextSpan(text: _isReg ? "Login" : "Register",
            style: const TextStyle(color: kPrimary, fontWeight: FontWeight.w700, fontSize: 13)),
      ])),
    )),
    const SizedBox(height:16),
    // ── social login divider ──
    Row(children:[
      const Expanded(child:Divider(color:kBorder)),
      Padding(padding:const EdgeInsets.symmetric(horizontal:10),child:Text("or continue with",style:TextStyle(color:kMuted,fontSize:11))),
      const Expanded(child:Divider(color:kBorder)),
    ]),
    const SizedBox(height:12),
    Row(children:[
      Expanded(child:_socialLoginBtn(
        onTap: _signInWithGoogle,
        label: "Google",
        icon: Icons.g_mobiledata_rounded,
        iconColor: const Color(0xFFDB4437),
        bgColor: Colors.white,
        borderColor: const Color(0xFFDDDDDD),
        textColor: const Color(0xFF444444),
      )),
      const SizedBox(width:10),
      Expanded(child:_socialLoginBtn(
        onTap: _signInWithFacebook,
        label: "Facebook",
        icon: Icons.facebook_rounded,
        iconColor: const Color(0xFF1877F2),
        bgColor: Colors.white,
        borderColor: const Color(0xFFDDDDDD),
        textColor: const Color(0xFF444444),
      )),
    ]),
    const SizedBox(height:12),
    _socialBar(),
    const SizedBox(height: 16),
  ]));

  Widget _merchantTab() => Padding(padding: const EdgeInsets.fromLTRB(22,4,22,22), child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
    if (_isMerchReg) ...[
      _fld(_mNameC, "Business / Owner Name", Icons.storefront_outlined), const SizedBox(height: 10),
      _fld(_mCityC, "City", Icons.location_city_outlined), const SizedBox(height: 10),
      _fld(_mAreaC, "Area / Locality", Icons.map_outlined), const SizedBox(height: 10),
    ],
    _ccPhoneRow(_merchCC, (v)=>setState(()=>_merchCC=v), _mPhoneC, "Registered Phone"),
    const SizedBox(height: 14),
    if (_isMerchReg) ...[
      Row(crossAxisAlignment:CrossAxisAlignment.start,children:[
        SizedBox(width:22,height:22,child:Checkbox(value:_agreedMerch,onChanged:(v)=>setState(()=>_agreedMerch=v??false),activeColor:kPrimary,materialTapTargetSize:MaterialTapTargetSize.shrinkWrap)),
        const SizedBox(width:6),
        Expanded(child:Wrap(children:[
          const Text("I agree to the ",style:TextStyle(fontSize:12,color:kMuted)),
          GestureDetector(onTap:()=>_showPolicy("Terms",Api.fetchTerms("merchant")),child:const Text("Terms",style:TextStyle(fontSize:12,color:kPrimary,decoration:TextDecoration.underline,fontWeight:FontWeight.bold))),
          const Text(", ",style:TextStyle(fontSize:12,color:kMuted)),
          GestureDetector(onTap:()=>_showPolicy("Privacy Policy",Api.fetchPolicy("privacy")),child:const Text("Privacy",style:TextStyle(fontSize:12,color:kPrimary,decoration:TextDecoration.underline,fontWeight:FontWeight.bold))),
          const Text(", ",style:TextStyle(fontSize:12,color:kMuted)),
          GestureDetector(onTap:()=>_showPolicy("KYC Policy",Api.fetchPolicy("kyc")),child:const Text("KYC",style:TextStyle(fontSize:12,color:kPrimary,decoration:TextDecoration.underline,fontWeight:FontWeight.bold))),
          const Text(" & ",style:TextStyle(fontSize:12,color:kMuted)),
          GestureDetector(onTap:()=>_showPolicy("Refund Policy",Api.fetchPolicy("refund")),child:const Text("Refund Policy",style:TextStyle(fontSize:12,color:kPrimary,decoration:TextDecoration.underline,fontWeight:FontWeight.bold))),
        ])),
      ]),
      const SizedBox(height:10),
    ],
    _btn(_isMerchReg ? "Register Business" : "Merchant Login", _loading ? null : (_isMerchReg ? (_agreedMerch ? _registerMerchant : null) : _loginMerchant)),
    if (_isMerchReg && !_agreedMerch) const Padding(padding:EdgeInsets.only(top:5),child:Text("Please accept the terms to continue",textAlign:TextAlign.center,style:TextStyle(color:Color(0xFFb56a3a),fontSize:11))),
    if (_msg.isNotEmpty) ...[const SizedBox(height:10), _msgWidget()],
    const SizedBox(height: 16),
    Center(child: GestureDetector(
      onTap: () => setState(() { _isMerchReg=!_isMerchReg; _msg=""; _agreedMerch=false; }),
      child: RichText(text: TextSpan(children: [
        TextSpan(text: _isMerchReg ? "Already registered? " : "New merchant? ",
            style: const TextStyle(color: kMuted, fontSize: 13)),
        TextSpan(text: _isMerchReg ? "Login" : "Register Business",
            style: const TextStyle(color: kPrimary, fontWeight: FontWeight.w700, fontSize: 13)),
      ])),
    )),
    const SizedBox(height:12),
    _socialBar(),
    const SizedBox(height: 16),
  ]));

  static const List<Map<String,String>> _countries = [
    {"code":"+91","flag":"🇮🇳","name":"India"},
    {"code":"+966","flag":"🇸🇦","name":"Saudi Arabia"},
  ];

  Widget _ccPhoneRow(String cc, void Function(String) onCCChange, TextEditingController ctrl, String hint) =>
    Row(children:[
      GestureDetector(
        onTap:()async{
          final picked = await showDialog<String>(context:context,builder:(ctx)=>SimpleDialog(
            title:const Text("Select Country",style:TextStyle(color:kPrimary,fontWeight:FontWeight.bold,fontSize:15)),
            children:_countries.map((c){
              final name=c["name"]!; final code=c["code"]!; final flag=c["flag"]!;
              return SimpleDialogOption(
                onPressed:(){Navigator.pop(ctx,code);},
                child:Row(children:[Text(flag,style:const TextStyle(fontSize:22)),const SizedBox(width:10),Text("$name ($code)",style:const TextStyle(fontSize:13))]),
              );
            }).toList(),
          ));
          if(picked!=null) onCCChange(picked);
        },
        child:Container(height:50,padding:const EdgeInsets.symmetric(horizontal:10),
          decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(12),border:Border.all(color:kBorder)),
          child:Row(mainAxisSize:MainAxisSize.min,children:[
            Text(_countries.firstWhere((c)=>c["code"]==cc,orElse:()=>_countries[0])["flag"]!,style:const TextStyle(fontSize:20)),
            const SizedBox(width:4),
            Text(cc,style:const TextStyle(color:kPrimary,fontWeight:FontWeight.w600,fontSize:13)),
            const Icon(Icons.arrow_drop_down,color:kMuted,size:18),
          ])),
      ),
      const SizedBox(width:8),
      Expanded(child:TextField(controller:ctrl,keyboardType:TextInputType.phone,
        maxLength:10,
        style:const TextStyle(fontSize:14,color:kText),
        decoration:InputDecoration(hintText:hint,counterText:"",hintStyle:const TextStyle(color:Color(0xFFb0c9c0)),
          filled:true,fillColor:Colors.white,contentPadding:const EdgeInsets.symmetric(vertical:14,horizontal:14),
          border:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kBorder)),
          enabledBorder:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kBorder)),
          focusedBorder:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kPrimary,width:2))))),
    ]);

  Widget _fld(TextEditingController c, String hint, IconData icon, {TextInputType type=TextInputType.text}) =>
    TextField(controller:c, keyboardType:type, style: const TextStyle(fontSize:14, color:kText),
      decoration: InputDecoration(hintText:hint, hintStyle:const TextStyle(color:Color(0xFFb0c9c0)), prefixIcon:Icon(icon,color:kMuted,size:20),
        filled:true, fillColor:Colors.white, contentPadding:const EdgeInsets.symmetric(vertical:14,horizontal:14),
        border:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kBorder)),
        enabledBorder:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kBorder)),
        focusedBorder:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kPrimary,width:2))));

  Widget _btn(String label, VoidCallback? onTap) => SizedBox(
    width: double.infinity,
    height: 52,
    child: ElevatedButton(
      onPressed: onTap,
      style: ElevatedButton.styleFrom(
        backgroundColor: kPrimary,
        disabledBackgroundColor: kAccent.withOpacity(.5),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        elevation: 0,
      ),
      child: _loading
        ? const SizedBox(width:22,height:22,child:CircularProgressIndicator(color:Colors.white,strokeWidth:2.5))
        : Text(label, style:const TextStyle(color:Colors.white,fontWeight:FontWeight.w800,fontSize:15,letterSpacing:0.3))));

  void _showPolicy(String title, Future<String> loader) async {
    final c = await loader;
    if (!mounted) return;
    showDialog(context:context, builder:(_)=>_PolicyDialog(title:title, body:c));
  }

  Widget _chip(IconData icon, String label) => Column(mainAxisSize:MainAxisSize.min,children:[
    Icon(icon,color:kPrimary,size:17),
    const SizedBox(height:3),
    Text(label,style:const TextStyle(color:kPrimary,fontSize:9,fontWeight:FontWeight.w600)),
  ]);

  Widget _msgWidget() => Container(
    padding: const EdgeInsets.symmetric(horizontal:12,vertical:8),
    decoration: BoxDecoration(color: _msgOk?const Color(0xFFd1f0e0):const Color(0xFFfde8e6), borderRadius:BorderRadius.circular(8)),
    child: Text(_msg, textAlign:TextAlign.center,
        style: TextStyle(color:_msgOk?const Color(0xFF1a6640):Colors.red.shade700, fontSize:12.5)));

  // ── Social login helpers ──
  Widget _socialLoginBtn({
    required VoidCallback onTap,
    required String label,
    required IconData icon,
    required Color iconColor,
    required Color bgColor,
    required Color borderColor,
    required Color textColor,
  }) => GestureDetector(
    onTap: onTap,
    child: Container(
      padding: const EdgeInsets.symmetric(vertical:11),
      decoration: BoxDecoration(
        color: bgColor,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color:borderColor, width:1.2),
        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.05),blurRadius:4,offset:const Offset(0,2))],
      ),
      child: Row(mainAxisAlignment:MainAxisAlignment.center, children:[
        Icon(icon, color:iconColor, size:20),
        const SizedBox(width:6),
        Text(label, style:TextStyle(color:textColor, fontWeight:FontWeight.w600, fontSize:13)),
      ]),
    ),
  );

  void _signInWithGoogle() {
    // TODO: integrate google_sign_in package
    // For now show a snackbar
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text("Google Sign-In coming soon!"), backgroundColor: kPrimary));
  }

  void _signInWithFacebook() {
    // TODO: integrate flutter_facebook_auth package
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text("Facebook Sign-In coming soon!"), backgroundColor: Color(0xFF1877F2)));
  }

    Widget _socialBar() => FutureBuilder<Map<String,dynamic>>(
    future: _socialFuture,
    builder: (ctx, snap) {
      final links = snap.data ?? {};
      final wa    = (links["whatsapp"]  ?? "") as String;
      final insta = (links["instagram"] ?? "") as String;
      final fb    = (links["facebook"]  ?? "") as String;
      final yt    = (links["youtube"]   ?? "") as String;
      // Always show the bar — gray if not loaded yet, colored when loaded
      final anySet = wa.isNotEmpty || insta.isNotEmpty || fb.isNotEmpty || yt.isNotEmpty;
      return Center(child: Row(mainAxisSize: MainAxisSize.min, children: [
        _socialIcon(wa.isNotEmpty?"https://wa.me/$wa":"", const Color(0xFF25D366),
          Icons.chat_rounded, "WhatsApp", active: wa.isNotEmpty),
        _socialIcon(insta.isNotEmpty?insta:"", const Color(0xFFE1306C),
          Icons.camera_alt_rounded, "Instagram", active: insta.isNotEmpty),
        _socialIcon(fb.isNotEmpty?fb:"", const Color(0xFF1877F2),
          Icons.facebook_rounded, "Facebook", active: fb.isNotEmpty),
        _socialIcon(yt.isNotEmpty?yt:"", const Color(0xFFFF0000),
          Icons.play_circle_filled_rounded, "YouTube", active: yt.isNotEmpty),
      ]));
    },
  );

  Widget _socialIcon(String url, Color bg, IconData icon, String label, {bool active = true}) => GestureDetector(
    onTap: () async {
      if (url.isEmpty || !active) return;
      String cleanUrl = url.trim();
      // Handle WhatsApp — strip to digits and build wa.me URL
      if (label == "WhatsApp") {
        final numOnly = cleanUrl.replaceAll(RegExp(r'[^0-9]'), '');
        if (numOnly.length >= 10) {
          cleanUrl = "https://wa.me/$numOnly";
        } else if (!cleanUrl.startsWith("http")) {
          cleanUrl = "https://wa.me/$cleanUrl";
        }
      } else {
        // Add https:// if missing
        if (!cleanUrl.startsWith("http://") && !cleanUrl.startsWith("https://")) {
          cleanUrl = "https://$cleanUrl";
        }
      }
      try {
        final uri = Uri.parse(cleanUrl);
        if (await canLaunchUrl(uri)) {
          await launchUrl(uri, mode: LaunchMode.externalApplication);
        } else {
          await launchUrl(uri, mode: LaunchMode.platformDefault);
        }
      } catch (e) {
        debugPrint("Social launch error ($label): $e");
      }
    },
    child: Opacity(
      opacity: active ? 1.0 : 0.35,
      child: Container(
        margin:const EdgeInsets.symmetric(horizontal:5),
        padding:const EdgeInsets.all(8),
        decoration:BoxDecoration(color:bg.withOpacity(.12),shape:BoxShape.circle),
        child:Icon(icon,color:bg,size:26),
      ),
    ),
  );

}
// ─────────────────────── POLICY DIALOG ───────────────────────
class _PolicyDialog extends StatelessWidget {
  final String title; final String body;
  const _PolicyDialog({required this.title, required this.body});

  @override Widget build(BuildContext ctx) => Dialog(
    shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(20)),
    insetPadding:const EdgeInsets.symmetric(horizontal:16,vertical:40),
    child:Column(mainAxisSize:MainAxisSize.min,children:[
      Container(width:double.infinity,padding:const EdgeInsets.fromLTRB(18,16,10,14),
        decoration:const BoxDecoration(color:kPrimary,borderRadius:BorderRadius.vertical(top:Radius.circular(20))),
        child:Row(children:[
          const Icon(Icons.policy,color:Colors.white,size:20),
          const SizedBox(width:8),
          Expanded(child:Text(title,style:const TextStyle(color:Colors.white,fontWeight:FontWeight.bold,fontSize:15))),
          IconButton(icon:const Icon(Icons.close,color:Colors.white,size:20),onPressed:()=>Navigator.pop(ctx),padding:EdgeInsets.zero,constraints:const BoxConstraints()),
        ])),
      Flexible(child:SingleChildScrollView(padding:const EdgeInsets.all(18),child:body.isEmpty
        ? const Text("Policy content will be published soon.",style:TextStyle(color:kMuted))
        : _render(body))),
      Padding(padding:const EdgeInsets.fromLTRB(16,4,16,14),child:SizedBox(width:double.infinity,height:42,child:ElevatedButton(
        style:ElevatedButton.styleFrom(backgroundColor:kPrimary,shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(12))),
        onPressed:()=>Navigator.pop(ctx),
        child:const Text("Ok",style:TextStyle(color:Colors.white,fontWeight:FontWeight.bold))))),
    ]),
  );

  Widget _render(String text) {
    final lines = text.split("\n");
    return Column(crossAxisAlignment:CrossAxisAlignment.start,children:lines.map((l){
      final t=l.trim();
      if(t.isEmpty) return const SizedBox(height:6);
      if(t.startsWith("# ")) return Padding(padding:const EdgeInsets.only(bottom:10,top:4),child:Text(t.substring(2),style:const TextStyle(fontSize:17,fontWeight:FontWeight.w900,color:kPrimary)));
      if(t.startsWith("## ")) return Padding(padding:const EdgeInsets.only(bottom:6,top:10),child:Text(t.substring(3),style:const TextStyle(fontSize:14,fontWeight:FontWeight.bold,color:kText)));
      if(t.startsWith("- ")) return Padding(
        padding:const EdgeInsets.only(left:8,bottom:4),
        child:Row(crossAxisAlignment:CrossAxisAlignment.start,children:[
          const Text("•  ",style:TextStyle(color:kPrimary,fontWeight:FontWeight.bold)),
          Expanded(child:Text(t.substring(2),style:const TextStyle(color:kText,fontSize:12.5,height:1.5))),
        ]));
      return Padding(padding:const EdgeInsets.only(bottom:5),child:Text(t,style:const TextStyle(color:kText,fontSize:12.5,height:1.6)));
    }).toList());
  }
}

// ─────────────────────── MERCHANT HOME ───────────────────────
class MerchantHome extends StatefulWidget {
  final String token; final Map merchant;
  const MerchantHome({super.key, required this.token, required this.merchant});
  @override State<MerchantHome> createState() => _MerchantHomeState();
}
class _MerchantHomeState extends State<MerchantHome> {
  int _idx = 0;
  late List<Widget> _pages;
  @override void initState() {
    super.initState();
    _pages = [
      MerchantStoresPage(token: widget.token),
      MerchantDealsPage(token: widget.token),
      MerchantInvoicesPage(token: widget.token),
      MerchantTxnPage(token: widget.token),
      MerchantProfilePage(token: widget.token, merchant: widget.merchant),
    ];
  }
  @override Widget build(BuildContext context) => PopScope(
    canPop: false,
    child: Scaffold(
    body: _pages[_idx],
    bottomNavigationBar: BottomNavigationBar(
      currentIndex: _idx,
      onTap: (i) => setState(()=>_idx=i),
      selectedItemColor: kPrimary, unselectedItemColor: kMuted,
      type: BottomNavigationBarType.fixed,
      items: const [
        BottomNavigationBarItem(icon:Icon(Icons.store),label:"Stores"),
        BottomNavigationBarItem(icon:Icon(Icons.local_offer),label:"Deals"),
        BottomNavigationBarItem(icon:Icon(Icons.receipt_long),label:"Invoices"),
        BottomNavigationBarItem(icon:Icon(Icons.history),label:"Activity"),
        BottomNavigationBarItem(icon:Icon(Icons.person),label:"Profile"),
      ],
    ),
  ));
}

// ─────────── Merchant Stores Page ───────────
class MerchantStoresPage extends StatefulWidget {
  final String token;
  const MerchantStoresPage({super.key, required this.token});
  @override State<MerchantStoresPage> createState() => _MerchantStoresState();
}
class _MerchantStoresState extends State<MerchantStoresPage> {
  List<Map<String,dynamic>> stores = []; bool loading = true;

  @override void initState() { super.initState(); _load(); }

  Future<void> _load() async {
    setState(()=>loading=true);
    stores = List<Map<String,dynamic>>.from(await Api.getMerchantStores(widget.token));
    if (mounted) setState(()=>loading=false);
  }

  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor: kBg,
    appBar: AppBar(title:Row(children:[buildLogo(20,Colors.white),const SizedBox(width:8),const Text("My Stores")]),
        backgroundColor:kPrimary,foregroundColor:Colors.white),
    floatingActionButton: FloatingActionButton.extended(
        backgroundColor:kPrimary, foregroundColor:Colors.white,
        icon:const Icon(Icons.add), label:const Text("Add Store"),
        onPressed:()=>Navigator.push(context,_route(AddEditStorePage(token:widget.token))).then((_)=>_load())),
    body: loading ? const Center(child:CircularProgressIndicator(color:kPrimary)) :
      stores.isEmpty ? Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
        buildLogo(44,kAccent), const SizedBox(height:12),
        const Text("No stores yet",style:TextStyle(color:kMuted,fontSize:16)),
        const SizedBox(height:8), const Text("Tap + to add your first store",style:TextStyle(color:kMuted,fontSize:13)),
      ])) :
      RefreshIndicator(onRefresh:_load,child:ListView.builder(
        padding:const EdgeInsets.all(14),
        itemCount:stores.length,
        itemBuilder:(_,i){
          final s = stores[i] as Map;
          final status = s["status"]??"draft";
          Color sc = kMuted;
          String sl = status;
          if (status=="active") { sc=const Color(0xFF1a6640); sl="✅ Active"; }
          else if (status=="waiting_approval") { sc=const Color(0xFF856404); sl="⏳ Pending Approval"; }
          else if (status=="draft") { sc=kMuted; sl="📝 Draft"; }
          else if (status=="inactive") { sc=Colors.red.shade700; sl="❌ Inactive"; }
          return Card(elevation:2,margin:const EdgeInsets.only(bottom:12),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(14)),
            child:Padding(padding:const EdgeInsets.all(14),child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
              Row(children:[
                Expanded(child:Text(s["store_name"]??"",style:const TextStyle(fontWeight:FontWeight.bold,fontSize:15,color:kText))),
                Container(padding:const EdgeInsets.symmetric(horizontal:10,vertical:4),
                    decoration:BoxDecoration(color:sc.withOpacity(.12),borderRadius:BorderRadius.circular(20)),
                    child:Text(sl,style:TextStyle(color:sc,fontSize:11,fontWeight:FontWeight.w600))),
              ]),
              const SizedBox(height:6),
              Text("${s['city']??''}, ${s['area']??''}",style:const TextStyle(color:kMuted,fontSize:12)),
              Text(s["category"]??"",style:const TextStyle(color:kMuted,fontSize:12)),
              if ((s["deal_count"] as int? ?? 0) > 0) ...[
                const SizedBox(height:5),
                Container(
                  padding:const EdgeInsets.symmetric(horizontal:8,vertical:3),
                  decoration:BoxDecoration(color:const Color(0xFFFFF0D0),borderRadius:BorderRadius.circular(8),border:Border.all(color:const Color(0xFFE6A817))),
                  child:Row(mainAxisSize:MainAxisSize.min,children:[
                    const Icon(Icons.local_offer,size:12,color:Color(0xFFB87A00)),
                    const SizedBox(width:4),
                    Text("${s['deal_count']??0} Deal${((s['deal_count']??0)>1)?'s':''} Active",style:const TextStyle(color:Color(0xFFB87A00),fontSize:11,fontWeight:FontWeight.w700)),
                  ])),
              ],
              if ((s["subscription_end"]??'').isNotEmpty)
                Container(margin:const EdgeInsets.only(top:5),padding:const EdgeInsets.symmetric(horizontal:8,vertical:4),
                  decoration:BoxDecoration(
                    color: status=="active" ? const Color(0xFFd1f0e0) : const Color(0xFFFFF3CD),
                    borderRadius:BorderRadius.circular(8)),
                  child:Row(mainAxisSize:MainAxisSize.min,children:[
                    Icon(Icons.event_available,size:13,color: status=="active" ? const Color(0xFF1a6640) : const Color(0xFF856404)),
                    const SizedBox(width:4),
                    Text("${status=='active'?'Active till':'Expires'}: ${s['subscription_end']}",
                      style:TextStyle(color: status=="active" ? const Color(0xFF1a6640) : const Color(0xFF856404),fontSize:11.5,fontWeight:FontWeight.w700)),
                  ])),
              const SizedBox(height:10),
              Row(children:[
                if (status=="draft"||status=="inactive") Expanded(child:ElevatedButton.icon(
                  icon:const Icon(Icons.payment,size:16), label:const Text("Subscribe"),
                  style:ElevatedButton.styleFrom(backgroundColor:kPrimary,foregroundColor:Colors.white,shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(10))),
                  onPressed:()=>Navigator.push(context,_route(SubscribePage(token:widget.token,store:s))).then((_)=>_load()))),
                if (status=="waiting_approval") Expanded(child:Container(
                  padding:const EdgeInsets.symmetric(vertical:8),
                  alignment:Alignment.center,
                  child:const Text("⏳ Awaiting Admin Approval",style:TextStyle(color:Color(0xFF856404),fontSize:12,fontWeight:FontWeight.w600)))),
                if (status=="active") ...[
                  Expanded(child:OutlinedButton.icon(
                    icon:const Icon(Icons.edit,size:16,color:kPrimary), label:const Text("Edit",style:TextStyle(color:kPrimary)),
                    style:OutlinedButton.styleFrom(side:const BorderSide(color:kPrimary),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(10))),
                    onPressed:()=>Navigator.push(context,_route(AddEditStorePage(token:widget.token,store:s))).then((_)=>_load()))),
                  const SizedBox(width:8),
                  OutlinedButton.icon(
                    icon:const Icon(Icons.add_shopping_cart,size:16,color:kPrimary),label:const Text("Deals",style:TextStyle(color:kPrimary,fontSize:12)),
                    style:OutlinedButton.styleFrom(side:const BorderSide(color:kBorder),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(10))),
                    onPressed:()=>Navigator.push(context,_route(AddDealPage(token:widget.token,storeId:s["_id"]??"",storeName:s["store_name"]??"")))),
                ],
                if (s["status"]=="active") ...[const SizedBox(width:8),OutlinedButton.icon(
                  icon:Icon((s["qr_code"]??'').isNotEmpty?Icons.qr_code:Icons.crop_free,size:16,color:kPrimary),
                  label:Text((s["qr_code"]??'').isNotEmpty?"QR":"Gen QR",style:const TextStyle(color:kPrimary,fontSize:12)),
                  style:OutlinedButton.styleFrom(side:const BorderSide(color:kBorder),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(10))),
                  onPressed:(s["qr_code"]??'').isNotEmpty
                    ?()=>_showQR(context,s["store_name"]??"",s["qr_code"]??'')
                    :() async {
                        final sid = s["_id"]??"";
                        try {
                          final res = await Api._post("/merchant/stores/$sid/reset-qr",{},token:widget.token);
                          final qr = res["qr_code"]??"";
                          if(qr.isNotEmpty){ setState(()=>s["qr_code"]=qr); if(mounted)_showQR(context,s["store_name"]??"",qr); }
                        } catch(e){ if(mounted)ScaffoldMessenger.of(context).showSnackBar(SnackBar(content:Text("Failed: $e"))); }
                      })],
              ]),
            ])));
        },
      )),
  );

  void _showQR(BuildContext ctx, String name, String qr) => showDialog(context:ctx, builder:(_)=>AlertDialog(
    title:Text(name,style:const TextStyle(fontSize:15,color:kPrimary)),
    content:Column(mainAxisSize:MainAxisSize.min,children:[
      qr.startsWith("data:image") ? Image.memory(base64Decode(qr.split(",").last),width:220,height:220) : const Icon(Icons.qr_code,size:100),
      const SizedBox(height:8), const Text("Show this to customers to earn points",textAlign:TextAlign.center,style:TextStyle(color:kMuted,fontSize:12)),
    ]),
    actions:[TextButton(onPressed:()=>Navigator.pop(ctx),child:const Text("Close",style:TextStyle(color:kPrimary)))],
  ));
}

// ─────────── Add/Edit Store Page ───────────

// ─────────────────────── INDIA STATES & CITIES ───────────────────────
const Map<String,List<String>> kIndiaCities = {
  "Andhra Pradesh": ["Visakhapatnam","Vijayawada","Guntur","Nellore","Kurnool","Rajahmundry","Tirupati","Kakinada","Kadapa","Anantapur"],
  "Arunachal Pradesh": ["Itanagar","Naharlagun","Pasighat"],
  "Assam": ["Guwahati","Silchar","Dibrugarh","Jorhat","Nagaon","Tinsukia"],
  "Bihar": ["Patna","Gaya","Bhagalpur","Muzaffarpur","Purnia","Darbhanga","Bihar Sharif","Arrah"],
  "Chhattisgarh": ["Raipur","Bhilai","Bilaspur","Korba","Durg","Rajnandgaon","Jagdalpur"],
  "Goa": ["Panaji","Margao","Vasco da Gama","Mapusa","Ponda"],
  "Gujarat": ["Ahmedabad","Surat","Vadodara","Rajkot","Bhavnagar","Jamnagar","Gandhinagar","Junagadh","Anand"],
  "Haryana": ["Faridabad","Gurugram","Panipat","Ambala","Yamunanagar","Rohtak","Hisar","Karnal","Sonipat","Panchkula"],
  "Himachal Pradesh": ["Shimla","Solan","Dharamshala","Mandi","Baddi","Palampur","Kullu"],
  "Jharkhand": ["Ranchi","Jamshedpur","Dhanbad","Bokaro","Deoghar","Hazaribagh"],
  "Karnataka": ["Bengaluru","Mysuru","Mangaluru","Hubli","Dharwad","Belagavi","Kalaburagi","Ballari","Vijayapura","Shivamogga","Tumkur","Davangere","Hassan","Udupi"],
  "Kerala": ["Thiruvananthapuram","Kochi","Kozhikode","Thrissur","Kollam","Kannur","Palakkad","Alappuzha","Malappuram","Kottayam"],
  "Madhya Pradesh": ["Bhopal","Indore","Jabalpur","Gwalior","Ujjain","Sagar","Dewas","Satna","Ratlam","Rewa"],
  "Maharashtra": ["Mumbai","Pune","Nagpur","Nashik","Thane","Aurangabad","Solapur","Kolhapur","Amravati","Nanded","Sangli","Malegaon","Jalgaon","Akola","Latur"],
  "Manipur": ["Imphal","Thoubal","Bishnupur","Churachandpur"],
  "Meghalaya": ["Shillong","Tura","Jowai"],
  "Mizoram": ["Aizawl","Lunglei","Champhai"],
  "Nagaland": ["Kohima","Dimapur","Mokokchung"],
  "Odisha": ["Bhubaneswar","Cuttack","Rourkela","Berhampur","Sambalpur","Puri","Balasore"],
  "Punjab": ["Ludhiana","Amritsar","Jalandhar","Patiala","Bathinda","Mohali","Firozpur","Hoshiarpur"],
  "Rajasthan": ["Jaipur","Jodhpur","Kota","Bikaner","Ajmer","Udaipur","Bhilwara","Alwar","Bharatpur","Sikar"],
  "Sikkim": ["Gangtok","Namchi","Gyalshing"],
  "Tamil Nadu": ["Chennai","Coimbatore","Madurai","Tiruchirappalli","Salem","Tirunelveli","Vellore","Erode","Thoothukudi","Tiruppur","Dindigul","Thanjavur"],
  "Telangana": ["Hyderabad","Warangal","Nizamabad","Karimnagar","Ramagundam","Khammam","Mahbubnagar","Nalgonda","Adilabad"],
  "Tripura": ["Agartala","Dharmanagar","Udaipur"],
  "Uttar Pradesh": ["Lucknow","Kanpur","Agra","Varanasi","Prayagraj","Meerut","Bareilly","Aligarh","Ghaziabad","Noida","Mathura","Moradabad","Gorakhpur"],
  "Uttarakhand": ["Dehradun","Haridwar","Roorkee","Haldwani","Rishikesh","Nainital","Kashipur","Rudrapur"],
  "West Bengal": ["Kolkata","Asansol","Siliguri","Durgapur","Bardhaman","Malda","Baharampur","Kharagpur"],
  "Delhi": ["New Delhi","Dwarka","Rohini","Pitampura","Laxmi Nagar","Janakpuri","Saket","Karol Bagh","Connaught Place"],
  "Jammu and Kashmir": ["Srinagar","Jammu","Anantnag","Baramulla","Sopore","Kathua"],
  "Ladakh": ["Leh","Kargil"],
  "Andaman and Nicobar Islands": ["Port Blair","Diglipur","Rangat"],
  "Chandigarh": ["Chandigarh"],
  "Dadra and Nagar Haveli and Daman and Diu": ["Daman","Diu","Silvassa"],
  "Lakshadweep": ["Kavaratti","Agatti"],
  "Puducherry": ["Puducherry","Karaikal","Mahe","Yanam"],
};
const List<String> kIndiaStates = [
  "Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh",
  "Goa","Gujarat","Haryana","Himachal Pradesh","Jharkhand",
  "Karnataka","Kerala","Madhya Pradesh","Maharashtra","Manipur",
  "Meghalaya","Mizoram","Nagaland","Odisha","Punjab",
  "Rajasthan","Sikkim","Tamil Nadu","Telangana","Tripura",
  "Uttar Pradesh","Uttarakhand","West Bengal","Delhi",
  "Jammu and Kashmir","Ladakh","Andaman and Nicobar Islands",
  "Chandigarh","Dadra and Nagar Haveli and Daman and Diu",
  "Lakshadweep","Puducherry",
];

class AddEditStorePage extends StatefulWidget {
  final String token; final Map? store;
  const AddEditStorePage({super.key, required this.token, this.store});
  @override State<AddEditStorePage> createState() => _AddEditStoreState();
}
class _AddEditStoreState extends State<AddEditStorePage> {
  final _name = TextEditingController();
  final _area = TextEditingController(); final _addr = TextEditingController();
  final _phone= TextEditingController(); final _lat  = TextEditingController();
  final _lng  = TextEditingController();
  String _category = ""; String? _imgB64; String? _img2B64; bool _loading = false; String _msg = "";
  List<String> _categories = [];
  String? _selState; String? _selCity;
  final _about = TextEditingController();

  bool get _isEdit => widget.store != null;

  @override void initState() {
    super.initState();
    _loadCategories();
    if (_isEdit) {
      _populateFromStore(widget.store!);
      // Also fetch the full store detail to get image2 + state/city (list API may omit them)
      _fetchFullStoreDetail();
    }
  }

  void _populateFromStore(Map s) {
    _name.text  = s["store_name"]??"";
    final rawState = (s["state"]??"").toString().trim();
    final rawCity  = (s["city"]??"").toString().trim();
    _selState   = rawState.isNotEmpty ? rawState : null;
    _selCity    = rawCity.isNotEmpty  ? rawCity  : null;
    _area.text  = s["area"]??"";       _addr.text = s["address"]??"";
    _phone.text = s["phone"]??"";      _lat.text  = s["lat"]??"";
    _lng.text   = s["lng"]??"";        _category  = s["category"]??"";
    if ((s["image"]??'').isNotEmpty) _imgB64 = s["image"];
    if ((s["image2"]??'').isNotEmpty) _img2B64 = s["image2"];
    _about.text = s["about"]??"";
  }

  Future<void> _fetchFullStoreDetail() async {
    try {
      final storeId = widget.store!["_id"]?.toString() ?? "";
      if (storeId.isEmpty) return;
      final detail = await Api.getMerchantStoreDetail(widget.token, storeId);
      if (mounted && detail != null) {
        setState(() => _populateFromStore(detail));
      }
    } catch (_) {}
  }
  @override void dispose() { _name.dispose();_area.dispose();_addr.dispose();_phone.dispose();_lat.dispose();_lng.dispose();_about.dispose(); super.dispose(); }

  Future<void> _loadCategories() async { _categories = await Api.fetchCategories(); if (mounted) setState((){}); }

  Future<void> _pickImage() async {
    final picker = ImagePicker();
    final img = await picker.pickImage(source: ImageSource.gallery, imageQuality: 70, maxWidth: 800);
    if (img == null) return;
    final bytes = await File(img.path).readAsBytes();
    setState(() => _imgB64 = "data:image/jpeg;base64,${base64Encode(bytes)}");
  }


  Future<void> _pickImage2() async {
    final picker = ImagePicker();
    final img = await picker.pickImage(source: ImageSource.gallery, imageQuality: 75, maxWidth: 800);
    if (img == null) return;
    final bytes = await File(img.path).readAsBytes();
    setState(() => _img2B64 = "data:image/jpeg;base64,${base64Encode(bytes)}");
  }

  Future<void> _save() async {
    if (_name.text.trim().isEmpty) { setState(()=>_msg="Store name required"); return; }
    setState(()=>_loading=true); _msg="";
    final data = {
      "store_name":_name.text.trim(),"category":_category,
      "state":_selState??"","city":_selCity??"","area":_area.text.trim(),
      "address":_addr.text.trim(),"phone":_phone.text.trim(),
      "lat":_lat.text.trim(),"lng":_lng.text.trim(),
      "about":_about.text.trim(),
      if(_imgB64!=null)"image":_imgB64,
      if(_img2B64!=null)"image2":_img2B64,
    };
    try {
      if (_isEdit) await Api.updateMerchantStore(widget.token, widget.store!["_id"], data);
      else         await Api.createMerchantStore(widget.token, data);
      if (!mounted) return;
      Navigator.pop(context);
    } catch(e) { setState(()=>_msg=e.toString().replaceAll("Exception: ","")); }
    if (mounted) setState(()=>_loading=false);
  }

  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor: kBg,
    appBar: AppBar(title:Text(_isEdit?"Edit Store":"Add Store"),backgroundColor:kPrimary,foregroundColor:Colors.white),
    body: SingleChildScrollView(padding:const EdgeInsets.all(18),child:Column(crossAxisAlignment:CrossAxisAlignment.stretch,children:[
      _field(_name,"Store Name *",Icons.store),
      const SizedBox(height:12),
      DropdownButtonFormField<String>(value:_category.isEmpty?null:_category,
        items:[..._categories.map((c)=>DropdownMenuItem<String>(value:c,child:Text(c)))],
        onChanged:(v)=>setState(()=>_category=v??''),
        decoration:_dec("Category",Icons.category),
        hint:const Text("Select category")),
      const SizedBox(height:12),
      DropdownButtonFormField<String>(
        isExpanded: true,
        value: kIndiaStates.contains(_selState)?_selState:null,
        items: kIndiaStates.map((s)=>DropdownMenuItem<String>(value:s,child:Text(s,style:const TextStyle(fontSize:13),overflow:TextOverflow.ellipsis))).toList(),
        onChanged:(v)=>setState((){_selState=v;_selCity=null;}),
        decoration:_dec("State *",Icons.map),
        hint:const Text("Select State",overflow:TextOverflow.ellipsis)),
      const SizedBox(height:12),
      DropdownButtonFormField<String>(
        isExpanded: true,
        value: (_selState!=null && (kIndiaCities[_selState]??[]).contains(_selCity))?_selCity:null,
        items: (_selState==null?[]:kIndiaCities[_selState]??[]).map((c)=>DropdownMenuItem<String>(value:c,child:Text(c,style:const TextStyle(fontSize:13),overflow:TextOverflow.ellipsis))).toList(),
        onChanged:(v)=>setState(()=>_selCity=v),
        decoration:_dec("City *",Icons.location_city),
        hint:Text(_selState==null?"Select State first":"Select City",overflow:TextOverflow.ellipsis)),
      const SizedBox(height:12),
      _field(_area,"Area / Locality",Icons.my_location),
      const SizedBox(height:12),
      _field(_addr,"Full Address",Icons.home),
      const SizedBox(height:12),
      _field(_phone,"Phone",Icons.phone,type:TextInputType.phone,maxLen:10),
      const SizedBox(height:12),
      Row(children:[Expanded(child:_field(_lat,"Latitude (optional)",Icons.gps_fixed,type:TextInputType.number)),const SizedBox(width:10),Expanded(child:_field(_lng,"Longitude (optional)",Icons.gps_not_fixed,type:TextInputType.number))]),
      const SizedBox(height:16),
      // Image picker
      GestureDetector(onTap:_pickImage,child:Container(height:150,decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(12),border:Border.all(color:kBorder,style:BorderStyle.solid)),
        child: _imgB64!=null
            ? ClipRRect(borderRadius:BorderRadius.circular(12),child:Image.memory(base64Decode(_imgB64!.split(",").last),fit:BoxFit.cover,width:double.infinity))
            : Column(mainAxisAlignment:MainAxisAlignment.center,children:[const Icon(Icons.add_a_photo,color:kMuted,size:36),const SizedBox(height:8),const Text("Image 1 — Main display card image",style:TextStyle(color:kMuted,fontSize:13))]))),
      const SizedBox(height:12),
      // Second image picker — optional logo
      const Text("Upload Logo [Optional]",style:TextStyle(color:kMuted,fontSize:12,fontWeight:FontWeight.w600)),
      const SizedBox(height:6),
      GestureDetector(onTap:_pickImage2,child:Container(height:110,decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(12),border:Border.all(color:kBorder,style:BorderStyle.solid)),
        child: _img2B64!=null
            ? Stack(children:[
                ClipRRect(borderRadius:BorderRadius.circular(12),child:Image.memory(base64Decode(_img2B64!.split(",").last),fit:BoxFit.cover,width:double.infinity,height:110)),
                Positioned(top:6,right:6,child:GestureDetector(
                  onTap:(){setState(()=>_img2B64=null);},
                  child:Container(padding:const EdgeInsets.all(4),decoration:BoxDecoration(color:Colors.black54,shape:BoxShape.circle),child:const Icon(Icons.close,color:Colors.white,size:14)))),
              ])
            : Column(mainAxisAlignment:MainAxisAlignment.center,children:[const Icon(Icons.image_outlined,color:kMuted,size:32),const SizedBox(height:6),const Text("Tap to upload logo",style:TextStyle(color:kMuted,fontSize:12))]))),
      const SizedBox(height:12),
      // About field
      TextField(controller:_about, maxLines:4, keyboardType:TextInputType.multiline,
        decoration:_dec("About this store (description shown to customers)",Icons.info_outline).copyWith(
          alignLabelWithHint:true)),
      const SizedBox(height:20),
      SizedBox(height:50,child:ElevatedButton(
        onPressed:_loading?null:_save,
        style:ElevatedButton.styleFrom(backgroundColor:kPrimary,shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(14))),
        child:_loading?const SizedBox(width:20,height:20,child:CircularProgressIndicator(color:Colors.white,strokeWidth:2)):
            Text(_isEdit?"Save Changes":"Create Store",style:const TextStyle(color:Colors.white,fontSize:15,fontWeight:FontWeight.w700)))),
      if (_msg.isNotEmpty)...[const SizedBox(height:10),Text(_msg,textAlign:TextAlign.center,style:TextStyle(color:Colors.red.shade700,fontSize:13))],
      const SizedBox(height:24),
    ])),
  );

  Widget _field(TextEditingController c,String hint,IconData icon,{TextInputType type=TextInputType.text,int? maxLen}) =>
    TextField(controller:c,keyboardType:type,maxLength:maxLen,decoration:_dec(hint,icon).copyWith(counterText:maxLen!=null?"":null));
  InputDecoration _dec(String hint, IconData icon) => InputDecoration(
    hintText:hint,
    prefixIcon:Icon(icon,color:kMuted,size:20),
    filled:true,
    fillColor:Colors.white,
    contentPadding:const EdgeInsets.symmetric(vertical:14,horizontal:14),
    border:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kBorder)),
    enabledBorder:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kBorder)),
    focusedBorder:OutlineInputBorder(borderRadius:BorderRadius.circular(12),borderSide:const BorderSide(color:kPrimary,width:2)));
}

// ─────────── Subscribe Page ───────────
class SubscribePage extends StatefulWidget {
  final String token; final Map store;
  const SubscribePage({super.key,required this.token,required this.store});
  @override State<SubscribePage> createState() => _SubscribeState();
}
class _SubscribeState extends State<SubscribePage> {
  Map<String,dynamic> _pendingOrder = {};
  List _plans = []; bool _loading = true; String _selectedPlan = ""; String _fromDate = "";
  Map? _selectedPlanData; String _msg = "";
  final TextEditingController _discC = TextEditingController();
  String? _appliedCode; double _discountValue = 0; bool _validatingDisc = false; String _discMsg = "";

  // Razorpay instance must live for the lifetime of this page so its native
  // callbacks (success/error/external_wallet) are not garbage-collected while
  // the Razorpay native checkout activity is on top. Creating it inside a
  // local function caused the app to silently return after "Pay Now" on
  // newer Android versions.
  late final Razorpay _razorpay;

  @override void initState() {
    super.initState();
    _razorpay = Razorpay();
    _razorpay.on(Razorpay.EVENT_PAYMENT_SUCCESS, _onPaySuccess);
    _razorpay.on(Razorpay.EVENT_PAYMENT_ERROR,   _onPayError);
    _razorpay.on(Razorpay.EVENT_EXTERNAL_WALLET, _onExtWallet);
    _loadPlans();
    _fromDate = DateTime.now().toIso8601String().substring(0,10);
  }

  @override void dispose() {
    _razorpay.clear();
    _discC.dispose();
    super.dispose();
  }

  Future<void> _openRazorpay(Map<String,dynamic> order) async {
    try {
      final amountPaise = (order["amount"] as num?)?.toInt() ??
          ((double.tryParse(order["amount_display"]?.toString() ?? "0") ?? 0) * 100).round();
      final opts = {
        'key': RAZORPAY_KEY,
        'amount': amountPaise,
        'currency': 'INR',
        'order_id': order["razorpay_order_id"] ?? order["order_id"] ?? "",
        'name': 'Offro',
        'description': order["plan_label"] ?? 'Store Subscription',
        'prefill': {
          'contact': order["merchant_phone"] ?? "",
        },
        'image': 'https://offro-backend-production.up.railway.app/static/offro_logo.png',
        'theme': {'color': '#3E5F55'},
      };
      _razorpay.open(opts);
    } catch(e) {
      if (mounted) setState(() => _msg = 'Could not open payment: $e');
    }
  }

  Future<void> _onPaySuccess(PaymentSuccessResponse resp) async {
    if (!mounted) return;
    final payId = resp.paymentId ?? "";
    final ordId = resp.orderId ?? _pendingOrder["razorpay_order_id"] ?? "";
    final sig   = resp.signature ?? "";
    // Show "Confirming payment" dialog immediately so user sees action right away
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (_) => const Center(child: Card(
        color: Colors.white,
        child: Padding(padding: EdgeInsets.all(28), child: Column(mainAxisSize: MainAxisSize.min, children: [
          CircularProgressIndicator(color: kPrimary),
          SizedBox(height: 16),
          Text("Confirming payment...", style: TextStyle(fontWeight: FontWeight.w600, color: kText)),
          SizedBox(height: 4),
          Text("Please wait a moment", style: TextStyle(color: kMuted, fontSize: 12)),
        ])),
      )),
    );
    // Verify payment synchronously (with retries)
    String invoiceNo = payId;
    try {
      for (int attempt = 0; attempt < 3; attempt++) {
        try {
          final result = await Api.verifyPayment(widget.token, {
            "razorpay_payment_id": payId,
            "razorpay_order_id":   ordId,
            "razorpay_signature":  sig,
            "store_id": widget.store["_id"],
          });
          invoiceNo = result["invoice_no"]?.toString() ?? payId;
          break;
        } catch(e) {
          if (attempt < 2) await Future.delayed(Duration(seconds: attempt + 1));
        }
      }
    } catch (_) {}
    if (!mounted) return;
    // Dismiss the confirming dialog
    Navigator.of(context).pop();
    // Navigate immediately to success screen
    Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => PaymentSuccessScreen(
      storeName: widget.store["store_name"]?.toString() ?? "",
      invoiceNo: invoiceNo,
      onDone: () {},
    )));
  }

  void _onPayError(PaymentFailureResponse resp) {
    if (mounted) setState(() { final m = resp.message ?? ""; _msg = "Payment cancelled or failed: $m"; });
  }

  void _onExtWallet(ExternalWalletResponse resp) {
    if (mounted) setState(() { final w = resp.walletName ?? ""; _msg = "External wallet: $w"; });
  }

    Future<void> _validateDiscount() async {
    final code = _discC.text.trim();
    if (code.isEmpty) return;
    setState(()=>_validatingDisc=true);
    try {
      final r = await Api.validateDiscount(code);
      setState((){
        _appliedCode = r["code"];
        _discountValue = (r["value"] as num).toDouble();
        _discMsg = "✅ ₹${_discountValue.toStringAsFixed(0)} discount applied!";
      });
    } catch(e) {
      setState((){
        _appliedCode = null; _discountValue = 0;
        _discMsg = e.toString().replaceAll("Exception: ","");
      });
    }
    if (mounted) setState(()=>_validatingDisc=false);
  }

  Future<void> _loadPlans() async {
    _plans = await Api.getPlans(widget.token);
    if (_plans.isNotEmpty) { _selectedPlan = _plans[0]["id"]; _selectedPlanData = Map.from(_plans[0]); }
    if (mounted) setState(()=>_loading=false);
  }

  Future<void> _subscribe() async {
    if (_selectedPlan.isEmpty) return;
    setState(()=>_loading=true); _msg="";
    try {
      final order = await Api.initiateSubscription(widget.token, {
        "store_id":     widget.store["_id"],
        "plan":         _selectedPlan,
        "from_date":    _fromDate,
        if (_appliedCode != null) "discount_code": _appliedCode,
        if (_discountValue > 0) "discount_value": _discountValue,
      });
      if (!mounted) return;
      final payMode = order["pay_mode"] ?? "manual";
      // If amount is 0, always treat as manual (free plan / promo)
      final orderAmt = (order["amount"] as num?)?.toInt() ??
          ((double.tryParse(order["amount_display"]?.toString() ?? "0") ?? 0) * 100).round();

      // ── Manual / offline payment mode ──
      if (payMode == "manual" || orderAmt <= 0) {
        showDialog(context:context,barrierDismissible:false,builder:(ctx)=>AlertDialog(
          title:const Text("Subscription Request Sent",style:TextStyle(color:kPrimary,fontWeight:FontWeight.bold)),
          content:Column(mainAxisSize:MainAxisSize.min,crossAxisAlignment:CrossAxisAlignment.start,children:[
            _row("Store", widget.store["store_name"]??''),
            _row("Plan",  order["plan_label"]??''),
            _row("From",  order["from_date"]??''),
            _row("To",    order["end_date"]??''),
            const Divider(),
            _row("Base Price","₹${order['base_price']}"),
            _row("GST (${order['gst_percent']}%)","₹${order['gst_amount']}"),
            _row("Total Payable","₹${order['amount_display']}",bold:true),
            const SizedBox(height:12),
            Container(
              padding:const EdgeInsets.all(10),
              decoration:BoxDecoration(color:kLight.withOpacity(.4),borderRadius:BorderRadius.circular(8)),
              child:const Text(
                "✅  Your subscription request has been submitted.\n\nPlease pay the amount to your Offro representative.\nThe admin will activate your store once payment is confirmed.",
                style:TextStyle(fontSize:12,color:kPrimary,height:1.5),
              ),
            ),
          ]),
          actions:[
            ElevatedButton(
              style:ElevatedButton.styleFrom(backgroundColor:kPrimary,shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(10))),
              onPressed:() async {
                Navigator.pop(ctx);
                // If amount is 0, activate immediately via free endpoint
                if (orderAmt <= 0) {
                  try {
                    await Api.activateFreeSubscription(widget.token, {
                      "store_id":       widget.store["_id"]?.toString() ?? "",
                      "subscription_id": order["subscription_id"]?.toString() ?? "",
                    });
                  } catch(_) {}
                }
                if (context.mounted) Navigator.pop(context);
              },
              child:const Text("OK",style:TextStyle(color:Colors.white))),
          ],
        ));
        return;
      }

      // ── Razorpay online mode ──
      setState(() => _pendingOrder = Map<String,dynamic>.from(order));
      final planLbl = order["plan_label"]?.toString() ?? _selectedPlan;
      // Use num conversion to avoid toString() showing "0" on valid values
      final baseP   = (order["base_price"] as num?)?.toStringAsFixed(2) ?? _selectedPlanData?["price"]?.toString() ?? "0";
      final gstPct  = (order["gst_percent"] as num?)?.toString() ?? "18";
      final gstAmt  = (order["gst_amount"] as num?)?.toStringAsFixed(2) ?? "0";
      final totalD  = (order["amount_display"] as num?)?.toStringAsFixed(2) ?? (order["total"] as num?)?.toStringAsFixed(2) ?? "0";
      final fromD   = order["from_date"]?.toString() ?? "";
      final toD     = order["end_date"]?.toString() ?? "";
      showDialog(context:context,builder:(_)=>AlertDialog(
        title:const Text("Confirm Payment",style:TextStyle(color:kPrimary,fontWeight:FontWeight.bold)),
        content:Column(mainAxisSize:MainAxisSize.min,crossAxisAlignment:CrossAxisAlignment.start,children:[
          _row("Store", widget.store["store_name"]?.toString()??''),
          _row("Plan",  planLbl),
          _row("From",  fromD),
          _row("To",    toD),
          const Divider(),
          _row("Base Price","₹$baseP"),
          _row("GST ($gstPct%)","₹$gstAmt"),
          _row("Total","₹$totalD",bold:true),
          const SizedBox(height:12),
          Container(padding:const EdgeInsets.all(10),decoration:BoxDecoration(color:kLight.withOpacity(.4),borderRadius:BorderRadius.circular(8)),
            child:const Text("Razorpay checkout will open. After payment, admin will approve your store.",style:TextStyle(fontSize:12,color:kPrimary))),
        ]),
        actions:[
          TextButton(onPressed:()=>Navigator.pop(context),child:const Text("Cancel",style:TextStyle(color:kMuted))),
          ElevatedButton(
            style:ElevatedButton.styleFrom(backgroundColor:kPrimary),
            onPressed:() {
              Navigator.pop(context);
              _openRazorpay(_pendingOrder);
            },
            child:const Text("Pay Now",style:TextStyle(color:Colors.white))),
        ],
      ));
    } catch(e) {
      if (mounted) setState(()=>_msg=e.toString().replaceAll("Exception: ",""));
    }
    if (mounted) setState(()=>_loading=false);
  }

  Widget _row(String k,String v,{bool bold=false}) => Padding(padding:const EdgeInsets.symmetric(vertical:3),
    child:Row(children:[Expanded(child:Text(k,style:const TextStyle(color:kMuted,fontSize:13))),Text(v,style:TextStyle(fontSize:13,fontWeight:bold?FontWeight.bold:FontWeight.w500,color:bold?kPrimary:kText))]));

  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor:kBg,
    appBar:AppBar(title:const Text("Subscribe Store"),backgroundColor:kPrimary,foregroundColor:Colors.white),
    body:_loading?const Center(child:CircularProgressIndicator(color:kPrimary)):SingleChildScrollView(padding:const EdgeInsets.all(18),child:Column(crossAxisAlignment:CrossAxisAlignment.stretch,children:[
      Container(padding:const EdgeInsets.all(16),decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(14),border:Border.all(color:kBorder)),
        child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
          const Text("Store",style:TextStyle(color:kMuted,fontSize:11,fontWeight:FontWeight.w600)),
          Text(widget.store["store_name"]??"",style:const TextStyle(fontSize:16,fontWeight:FontWeight.bold,color:kText)),
          Text("${widget.store['city']??''}, ${widget.store['area']??''}",style:const TextStyle(color:kMuted,fontSize:12)),
        ])),
      const SizedBox(height:20),
      const Text("Select Plan",style:TextStyle(fontWeight:FontWeight.bold,color:kText,fontSize:15)),
      const SizedBox(height:10),
      ..._plans.map((p){
        final sel = _selectedPlan==p["id"];
        return GestureDetector(onTap:()=>setState((){_selectedPlan=p["id"];_selectedPlanData=Map.from(p);}),
          child:Container(margin:const EdgeInsets.only(bottom:10),padding:const EdgeInsets.all(16),
            decoration:BoxDecoration(color:sel?kPrimary:Colors.white,borderRadius:BorderRadius.circular(14),border:Border.all(color:sel?kPrimary:kBorder,width:sel?2:1)),
            child:Row(children:[
              Icon(sel?Icons.radio_button_checked:Icons.radio_button_unchecked,color:sel?Colors.white:kMuted),
              const SizedBox(width:12),
              Expanded(child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
                Text(p["label"],style:TextStyle(fontWeight:FontWeight.bold,color:sel?Colors.white:kText)),
                Text("₹${p['price']} + ${p['gst_percent']}% GST",style:TextStyle(fontSize:12,color:sel?kLight:kMuted)),
              ])),
              Text("₹${p['total']}",style:TextStyle(fontWeight:FontWeight.bold,fontSize:16,color:sel?Colors.white:kPrimary)),
            ])));
      }).toList(),
      const SizedBox(height:16),
      const Text("Start Date",style:TextStyle(fontWeight:FontWeight.bold,color:kText,fontSize:15)),
      const SizedBox(height:8),
      GestureDetector(
        onTap:() async {
          final d = await showDatePicker(context:context,initialDate:DateTime.now(),firstDate:DateTime.now(),lastDate:DateTime.now().add(const Duration(days:60)),
            builder:(ctx,child)=>Theme(data:ThemeData(colorScheme:const ColorScheme.light(primary:kPrimary)),child:child!));
          if (d!=null) setState(()=>_fromDate=d.toIso8601String().substring(0,10));
        },
        child:Container(padding:const EdgeInsets.all(14),decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(12),border:Border.all(color:kBorder)),
          child:Row(children:[const Icon(Icons.calendar_today,color:kPrimary,size:18),const SizedBox(width:10),Text(_fromDate,style:const TextStyle(color:kText,fontWeight:FontWeight.w600))]))),
      if (_selectedPlanData!=null)...[
        const SizedBox(height:16),
        Container(padding:const EdgeInsets.all(14),decoration:BoxDecoration(color:kLight.withOpacity(.5),borderRadius:BorderRadius.circular(12)),
          child:Column(children:[
            _row("Base Price","₹${_selectedPlanData!['price']}"),
            _row("GST (${_selectedPlanData!['gst_percent']}%)","₹${_selectedPlanData!['gst_amount']}"),
            const Divider(height:16),
            _row("Total Payable","₹${_selectedPlanData!['total']}",bold:true),
          ])),
      ],
      const SizedBox(height:16),
      // ── Discount Code ──
      Container(padding:const EdgeInsets.all(14),decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(12),border:Border.all(color:kBorder)),
        child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
          const Text("Have a discount code?",style:TextStyle(fontWeight:FontWeight.w600,color:kText,fontSize:13)),
          const SizedBox(height:8),
          Row(children:[
            Expanded(child:TextField(
              controller:_discC,
              textCapitalization:TextCapitalization.characters,
              decoration:InputDecoration(hintText:"Enter code",isDense:true,contentPadding:const EdgeInsets.symmetric(horizontal:10,vertical:10),border:OutlineInputBorder(borderRadius:BorderRadius.circular(8),borderSide:BorderSide(color:kBorder))),
            )),
            const SizedBox(width:8),
            ElevatedButton(
              onPressed:_validatingDisc?null:_validateDiscount,
              style:ElevatedButton.styleFrom(backgroundColor:kPrimary,shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(8)),padding:const EdgeInsets.symmetric(horizontal:14,vertical:10)),
              child:_validatingDisc?const SizedBox(width:16,height:16,child:CircularProgressIndicator(color:Colors.white,strokeWidth:2)):const Text("Apply",style:TextStyle(color:Colors.white,fontSize:13))),
          ]),
          if (_discMsg.isNotEmpty)...[const SizedBox(height:6),Text(_discMsg,style:TextStyle(fontSize:12,color:_appliedCode!=null?const Color(0xFF1a6640):Colors.red.shade700))],
          if (_appliedCode!=null && _discountValue>0)...[
            const SizedBox(height:6),
            Row(children:[const Text("Discount: ",style:TextStyle(fontSize:12,color:kMuted)),Text("- ₹${_discountValue.toStringAsFixed(0)}",style:const TextStyle(fontSize:12,color:Color(0xFF1a6640),fontWeight:FontWeight.bold)),
              const Spacer(),
              GestureDetector(onTap:(){setState((){_appliedCode=null;_discountValue=0;_discMsg="";_discC.clear();});},child:const Text("Remove",style:TextStyle(fontSize:11,color:Colors.red,decoration:TextDecoration.underline)))]),
          ],
        ])),
      const SizedBox(height:24),
      SizedBox(height:52,child:ElevatedButton(
        onPressed:_loading?null:_subscribe,
        style:ElevatedButton.styleFrom(backgroundColor:kPrimary,shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(14))),
        child:const Text("Proceed to Pay",style:TextStyle(color:Colors.white,fontSize:16,fontWeight:FontWeight.w700)))),
      if (_msg.isNotEmpty)...[const SizedBox(height:10),Text(_msg,textAlign:TextAlign.center,style:TextStyle(color:Colors.red.shade700,fontSize:13))],
      const SizedBox(height:24),
    ])),
  );
}

// ─────────── Merchant Invoices Page ───────────
class MerchantInvoicesPage extends StatefulWidget {
  final String token; const MerchantInvoicesPage({super.key,required this.token});
  @override State<MerchantInvoicesPage> createState() => _InvoicesState();
}
class _InvoicesState extends State<MerchantInvoicesPage> {
  List _invoices = []; bool _loading = true;
  @override void initState() { super.initState(); _load(); }
  Future<void> _load() async {
    if (mounted) setState(()=>_loading=true);
    _invoices = await Api.getInvoices(widget.token);
    if(mounted) setState(()=>_loading=false);
  }
  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor:kBg,
    appBar:AppBar(
      title:const Text("Invoices"),
      backgroundColor:kPrimary,
      foregroundColor:Colors.white,
      actions:[
        IconButton(icon:const Icon(Icons.refresh),onPressed:_load,tooltip:"Refresh"),
      ],
    ),
    body:_loading?const Center(child:CircularProgressIndicator(color:kPrimary)):
    RefreshIndicator(
      color:kPrimary,
      onRefresh:_load,
      child:_invoices.isEmpty
        ? ListView(children:[SizedBox(height:MediaQuery.of(context).size.height*0.4,child:const Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
            Icon(Icons.receipt_long_outlined,size:56,color:kAccent),
            SizedBox(height:12),
            Text("No invoices yet",style:TextStyle(color:kMuted,fontSize:15)),
            SizedBox(height:6),
            Text("Pull down to refresh",style:TextStyle(color:kMuted,fontSize:12)),
          ])))])
        : ListView.builder(padding:const EdgeInsets.all(14),itemCount:_invoices.length,itemBuilder:(_,i){
          final inv = _invoices[i] as Map;
          return Card(elevation:1,margin:const EdgeInsets.only(bottom:10),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(14)),
            child:Padding(padding:const EdgeInsets.all(16),child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
              Row(children:[
                const Icon(Icons.receipt_long,color:kPrimary,size:16),
                const SizedBox(width:6),
                Expanded(child:Text(inv["invoice_no"]??"",style:const TextStyle(fontWeight:FontWeight.bold,color:kPrimary,fontSize:13))),
                Text(inv["created_at"]??"",style:const TextStyle(color:kMuted,fontSize:11)),
              ]),
              const Divider(height:14),
              Text(inv["store_name"]??"",style:const TextStyle(fontWeight:FontWeight.bold,color:kText)),
              Text("Plan: ${inv['plan']??''}  •  ${inv['from_date']??''} – ${inv['end_date']??''}",style:const TextStyle(color:kMuted,fontSize:12)),
              const SizedBox(height:6),
              Row(mainAxisAlignment:MainAxisAlignment.spaceBetween,children:[
                Text("Base: ₹${inv['base_price']??0}  GST: ₹${inv['gst']??0}",style:const TextStyle(color:kMuted,fontSize:12)),
                Text("₹${inv['total']??0}",style:const TextStyle(fontWeight:FontWeight.bold,color:kPrimary,fontSize:15)),
              ]),
            ])));
        }),
    ),
  );
}

// ─────────── Merchant Transactions Page ───────────
class MerchantTxnPage extends StatefulWidget {
  final String token; const MerchantTxnPage({super.key,required this.token});
  @override State<MerchantTxnPage> createState() => _TxnState();
}
class _TxnState extends State<MerchantTxnPage> {
  List _txns = []; bool _loading = true;
  @override void initState() { super.initState(); _load(); }
  Future<void> _load() async { _txns = await Api.getMerchantTransactions(widget.token); if(mounted)setState(()=>_loading=false); }
  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor:kBg,
    appBar:AppBar(title:const Text("Activity"),backgroundColor:kPrimary,foregroundColor:Colors.white),
    body:_loading?const Center(child:CircularProgressIndicator(color:kPrimary)):
    _txns.isEmpty?const Center(child:Text("No activity yet",style:TextStyle(color:kMuted))):
    ListView.builder(padding:const EdgeInsets.all(14),itemCount:_txns.length,itemBuilder:(_,i){
      final t = _txns[i] as Map;
      final isPayment = t["type"]=="subscription";
      return Container(margin:const EdgeInsets.only(bottom:8),padding:const EdgeInsets.all(14),
        decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(12),border:Border.all(color:kBorder)),
        child:Row(children:[
          CircleAvatar(backgroundColor:isPayment?kLight:kBg,radius:20,child:Icon(isPayment?Icons.payment:Icons.store,color:kPrimary,size:18)),
          const SizedBox(width:12),
          Expanded(child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
            Text(t["description"]??"",style:const TextStyle(fontWeight:FontWeight.bold,color:kText,fontSize:13)),
            Text(t["date"]??"",style:const TextStyle(color:kMuted,fontSize:11)),
          ])),
          if ((t["amount"]??0)>0) Text("₹${t['amount']}",style:const TextStyle(color:kPrimary,fontWeight:FontWeight.bold)),
        ]));
    }),
  );
}

// ─────────── Merchant Profile Page ───────────
class MerchantProfilePage extends StatefulWidget {
  final String token; final Map merchant;
  const MerchantProfilePage({super.key,required this.token,required this.merchant});
  @override State<MerchantProfilePage> createState() => _MerchantProfileState();
}
class _MerchantProfileState extends State<MerchantProfilePage> {
  String? _imgB64;
  bool _uploading = false;

  @override void initState() {
    super.initState();
    _imgB64 = widget.merchant["profile_image"] as String?;
  }

  Future<void> _pickProfileImage() async {
    final picker = ImagePicker();
    final img = await picker.pickImage(source: ImageSource.gallery, imageQuality: 70, maxWidth: 600);
    if (img == null) return;
    final bytes = await File(img.path).readAsBytes();
    final b64 = "data:image/jpeg;base64,${base64Encode(bytes)}";
    setState(() { _uploading = true; _imgB64 = b64; });
    try {
      await Api.updateMerchantProfile(widget.token, {"profile_image": b64});
    } catch(_) {}
    if (mounted) setState(() => _uploading = false);
  }

  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor:kBg,
    appBar:AppBar(title:const Text("Profile"),backgroundColor:kPrimary,foregroundColor:Colors.white),
    body:SingleChildScrollView(padding:const EdgeInsets.all(20),child:Column(crossAxisAlignment:CrossAxisAlignment.stretch,children:[
      Container(padding:const EdgeInsets.all(20),decoration:BoxDecoration(color:kPrimary,borderRadius:BorderRadius.circular(16)),
        child:Column(children:[
          GestureDetector(
            onTap: _pickProfileImage,
            child: Stack(alignment:Alignment.bottomRight,children:[
              CircleAvatar(radius:44, backgroundColor:kAccent,
                backgroundImage: _imgB64 != null && _imgB64!.startsWith("data:") ? MemoryImage(base64Decode(_imgB64!.split(",").last)) : null,
                child: _imgB64 == null ? buildLogo(38,kLight) : null),
              Container(width:26,height:26,decoration:const BoxDecoration(color:Colors.white,shape:BoxShape.circle),
                child: _uploading ? const Padding(padding:EdgeInsets.all(4),child:CircularProgressIndicator(strokeWidth:2,color:kPrimary)) : const Icon(Icons.camera_alt,size:16,color:kPrimary)),
            ])),
          const SizedBox(height:8),
          Text(widget.merchant["name"]??"",style:const TextStyle(color:Colors.white,fontSize:18,fontWeight:FontWeight.bold)),
          Text(widget.merchant["phone"]??"",style:const TextStyle(color:kAccent)),
          if((widget.merchant["city"]??'').isNotEmpty)
            Text("${widget.merchant['city']}, ${widget.merchant['area']??''}",style:const TextStyle(color:kAccent,fontSize:12)),
          const SizedBox(height:4),
          const Text("Tap photo to change",style:TextStyle(color:kLight,fontSize:10)),
        ])),
      const SizedBox(height:20),
      _tile(context,Icons.info_outline,"About Us",() => _openAbout(context)),
      _tile(context,Icons.description,"Terms & Conditions",() => _openPolicy(context,"Terms & Conditions",Api.getMerchantTerms())),
      _tile(context,Icons.privacy_tip,"Privacy Policy",() => _openPolicy(context,"Privacy Policy",Api.fetchPolicy("privacy"))),
      _tile(context,Icons.receipt,"Refund Policy",() => _openPolicy(context,"Refund Policy",Api.fetchPolicy("refund"))),
      _tile(context,Icons.badge,"KYC Policy",() => _openPolicy(context,"KYC Policy",Api.fetchPolicy("kyc"))),
      const SizedBox(height:8),
      ListTile(tileColor:const Color(0xFFe8faf0),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(12),side:const BorderSide(color:Color(0xFF25D366))),
        leading:const CircleAvatar(backgroundColor:Color(0xFF25D366),child:Icon(Icons.chat_bubble,color:Colors.white)),
        title:const Text("Contact Offro",style:TextStyle(fontWeight:FontWeight.bold,color:Color(0xFF1a7a3c))),
        subtitle:const Text("Chat with us on WhatsApp",style:TextStyle(fontSize:11,color:Color(0xFF25D366))),
        trailing:const Icon(Icons.chevron_right,color:Color(0xFF25D366)),
        onTap:()async{final s=await Api.getSocialLinks();
          final rawWa=s["whatsapp"]??"";
          if(rawWa.isNotEmpty){
            final digits=rawWa.replaceAll(RegExp(r'[^0-9]'),'');
            final waNum=digits.length>=10?(digits.length==10?"91$digits":digits):digits;
            await launchUrl(Uri.parse("https://wa.me/$waNum"),mode:LaunchMode.externalApplication);
          }}),
      const SizedBox(height:20),
      SizedBox(height:48,child:OutlinedButton.icon(
        icon:const Icon(Icons.logout,color:Colors.red),label:const Text("Logout",style:TextStyle(color:Colors.red)),
        style:OutlinedButton.styleFrom(side:const BorderSide(color:Colors.red),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(12))),
        onPressed:() async {await Prefs.clear();if(!context.mounted)return;Navigator.pushAndRemoveUntil(context,_route(const OnboardingScreen()),(_)=>false);})),
    ])),
  );

  Widget _tile(BuildContext ctx, IconData icon, String title, VoidCallback onTap) =>
    ListTile(tileColor:Colors.white,shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(12),side:const BorderSide(color:kBorder)),
      leading:CircleAvatar(backgroundColor:kLight,child:Icon(icon,color:kPrimary)),
      title:Text(title,style:const TextStyle(fontWeight:FontWeight.bold,color:kText)),
      trailing:const Icon(Icons.chevron_right,color:kPrimary),onTap:onTap,
      contentPadding:const EdgeInsets.symmetric(horizontal:14,vertical:4));

  void _openAbout(BuildContext ctx) async {
    final c = await Api.getAboutUs();
    if (!ctx.mounted) return;
    showDialog(context:ctx, builder:(_)=>_PolicyDialog(title:"About Us",body:c.isEmpty?"Offro connects local stores with customers through deals and loyalty points.":c));
  }

  void _openPolicy(BuildContext ctx, String title, Future<String> loader) {
    showDialog(context:ctx, barrierDismissible:false,
      builder:(_)=>FutureBuilder<String>(future:loader,
        builder:(c,snap){
          if(snap.connectionState!=ConnectionState.done) return const AlertDialog(content:SizedBox(height:80,child:Center(child:CircularProgressIndicator(color:kPrimary))));
          return _PolicyDialog(title:title,body:snap.data??"");
        }));
  }
}


// ─────────── User Profile Header (with image upload) ───────────
class _UserProfileHeader extends StatefulWidget {
  final String token, name, phone;
  const _UserProfileHeader({required this.token, required this.name, required this.phone});
  @override State<_UserProfileHeader> createState() => _UserProfileHeaderState();
}
class _UserProfileHeaderState extends State<_UserProfileHeader> {
  String? _imgB64;
  bool _uploading = false;

  @override void initState() {
    super.initState();
    _loadProfileImage();
  }

  Future<void> _loadProfileImage() async {
    try {
      final d = await Api.getMe(widget.token);
      if (mounted && d != null && d["profile_image"] != null) setState(()=>_imgB64=d["profile_image"]);
    } catch(_){}
  }

  Future<void> _pickImage() async {
    final picker = ImagePicker();
    final img = await picker.pickImage(source: ImageSource.gallery, imageQuality: 70, maxWidth: 600);
    if (img == null) return;
    final bytes = await File(img.path).readAsBytes();
    final b64 = "data:image/jpeg;base64,${base64Encode(bytes)}";
    setState(() { _uploading = true; _imgB64 = b64; });
    try { await Api.updateUserProfile(widget.token, {"profile_image": b64}); } catch(_) {}
    if (mounted) setState(() => _uploading = false);
  }

  @override Widget build(BuildContext context) => ListTile(
    leading: GestureDetector(
      onTap: _pickImage,
      child: Stack(alignment:Alignment.bottomRight, children:[
        CircleAvatar(radius:24, backgroundColor:kLight,
          backgroundImage: _imgB64 != null && _imgB64!.startsWith("data:")
            ? MemoryImage(base64Decode(_imgB64!.split(",").last)) : null,
          child: _imgB64 == null ? const Icon(Icons.person, color:kPrimary) : null),
        Container(width:16,height:16,decoration:const BoxDecoration(color:kPrimary,shape:BoxShape.circle),
          child: _uploading ? const Padding(padding:EdgeInsets.all(2),child:CircularProgressIndicator(strokeWidth:1.5,color:Colors.white)) : const Icon(Icons.camera_alt,size:10,color:Colors.white)),
      ])),
    title: Text(widget.name, style:const TextStyle(fontWeight:FontWeight.bold)),
    subtitle: Text(widget.phone),
  );
}

// ─────────────────────── USER HOME ───────────────────────
class HomeScreen extends StatefulWidget {
  final String token,name,phone,savedCity;
  const HomeScreen({super.key,required this.token,required this.name,required this.phone,required this.savedCity});
  @override State<HomeScreen> createState() => _HomeState();
}
class _HomeState extends State<HomeScreen> {
  String city="Detecting..."; bool cityDone=false; bool _locationDenied=false; int _navIdx=0; String? _profilePhoto;
  bool _netError=false; bool _fetchFailed=false;
  double? _userLat; double? _userLng;
  bool _cityManual = false;
  final PageController _pc = PageController();
  int _page=0; int _voucherPage=0; Timer? _slide,_catTimer,_voucherTimer;
  final PageController _voucherPc = PageController(viewportFraction:0.94);
  String _cat="All"; bool _showCat=false; bool _loading=true;
  List<Map<String,dynamic>> _stores=[]; List<String> _cats=["All"];
  List<Map<String,dynamic>> _vouchers=[];
  List<Map<String,dynamic>> _sliders=[];
  int _sliderPage=0;
  final PageController _sliderPc = PageController();
  Timer? _sliderTimer;
  int _vp=0,_pp=0;

  // Stores marked "new_in_town" go to big carousel; they also appear in top stores
  // Auto-expire new_in_town after 20 days based on created_at
  bool _isStillNew(Map<String,dynamic> s) {
    if (s["is_new_in_town"] != true) return false;
    final raw = s["created_at"];
    if (raw == null) return true;
    try {
      final dt = DateTime.parse(raw.toString());
      return DateTime.now().difference(dt).inDays < 20;
    } catch(_) { return true; }
  }
  List<Map<String,dynamic>> _cachedNewStores = [];
  void _rebuildCaches() {
    _cachedNewStores = _stores.where((s)=>_isStillNew(s)).toList();
  }
  List<Map<String,dynamic>> get _newStores => _cachedNewStores;
  List<Map<String,dynamic>> get _topStores {
    final cat = _cat == "All" ? _stores : _stores.where((s)=>s["category"]==_cat).toList();
    return cat; // all stores including new ones appear in top stores
  }
  List<Map<String,dynamic>> get _sl => _cat=="All"?_stores:_stores.where((s)=>s["category"]==_cat).toList();

  @override void initState() { super.initState(); _loadWallet(); _initLoc(); _loadProfile(); _loadVouchers(); _loadSliders(); }

  Future<void> _loadWallet() async { try { final d=await Api.getWallet(widget.token); if(mounted)setState((){_vp=d["visit_points"]??0;_pp=d["pool_points"]??0;}); } catch(_){} }
  Future<void> _loadProfile() async { try { final d=await Api.getMe(widget.token); if(mounted&&d!=null) setState(()=>_profilePhoto=d["photo"]?.toString()); } catch(_){} }
  Future<void> _loadVouchers() async {
    try {
      final d = await Api.getGiftVouchers();
      if(mounted) setState((){
        _vouchers = List<Map<String,dynamic>>.from(d);
        for(int i=0;i<_vouchers.length;i++) _vouchers[i]["_idx"] = i;
      });
      _startVoucherSlide();
    } catch(e){ debugPrint("Vouchers err: \$e"); }
  }
  Future<void> _loadSliders() async {
    try {
      final d = await Api.getSliders();
      if(mounted) setState((){
        _sliders = List<Map<String,dynamic>>.from(d);
        // inject _idx for color cycling
        for(int i=0;i<_sliders.length;i++) _sliders[i]["_idx"] = i;
      });
      _startSliderAutoPlay();
    } catch(e){ debugPrint("Sliders err: \$e"); }
  }
  void _startSliderAutoPlay(){
    _sliderTimer?.cancel();
    if(_sliders.length>1){
      _sliderTimer=Timer.periodic(const Duration(seconds:4),(_){
        if(_sliderPc.hasClients){
          final next=(_sliderPage+1)%_sliders.length;
          _sliderPc.animateToPage(next,duration:const Duration(milliseconds:500),curve:Curves.easeInOut);
        }
      });
    }
  }
  void _startVoucherSlide() {
    _voucherTimer?.cancel();
    _voucherTimer = Timer.periodic(const Duration(seconds:3), (_) {
      if (_voucherPc.hasClients && _vouchers.isNotEmpty) {
        final next = (_voucherPage + 1) % _vouchers.length;
        _voucherPc.animateToPage(next, duration: const Duration(milliseconds:500), curve: Curves.easeInOut);
      }
    });
  }

  // ── City Picker ──────────────────────────────────────
  Future<void> _showCityPicker(BuildContext context) async {
    final states = kIndiaCities.keys.toList()..sort();
    String selState = "";
    String selCity  = "";
    // Pre-fill current city's state if possible
    for(final st in states){
      if(kIndiaCities[st]!.contains(city)){selState=st; selCity=city; break;}
    }
    await showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      builder: (_) => StatefulBuilder(builder: (ctx, setS) {
        final cities = selState.isNotEmpty?(kIndiaCities[selState]!.toList()..sort()):<String>[];
        return Container(
          padding: EdgeInsets.only(bottom: MediaQuery.of(ctx).viewInsets.bottom+20),
          decoration: const BoxDecoration(
            color: Colors.white,
            borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
          ),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20,16,20,8),
            child: Column(mainAxisSize:MainAxisSize.min, crossAxisAlignment:CrossAxisAlignment.start, children:[
              Row(children:[
                const Icon(Icons.location_on, color:kPrimary, size:20),
                const SizedBox(width:8),
                const Text("Select Your City", style:TextStyle(fontSize:17,fontWeight:FontWeight.w800,color:kText)),
                const Spacer(),
                // GPS button — refresh to live location
                IconButton(
                  icon: const Icon(Icons.gps_fixed, color:kPrimary),
                  tooltip:"Use GPS",
                  onPressed: () async {
                    Navigator.pop(ctx);
                    setState((){_cityManual=false; city="Detecting...";});
                    final det = await detectCity();
                    try {
                      final pos = await Geolocator.getCurrentPosition(desiredAccuracy:LocationAccuracy.medium)
                          .timeout(const Duration(seconds:10));
                      if(mounted) setState((){_userLat=pos.latitude; _userLng=pos.longitude;});
                    } catch(_){}
                    if(!mounted) return;
                    setState((){city=det; _locationDenied=false; _cityManual=false;});
                    await Prefs.saveCity(det);
                    await Api.updateCity(widget.token, det);
                    await _fetchStores(det);
                  },
                ),
              ]),
              const Divider(height:1),
              const SizedBox(height:14),
              // State dropdown
              const Text("State", style:TextStyle(fontSize:13,fontWeight:FontWeight.w600,color:kMuted)),
              const SizedBox(height:6),
              DropdownButtonFormField<String>(
                value: selState.isNotEmpty?selState:null,
                hint: const Text("Select State"),
                isExpanded: true,
                decoration: InputDecoration(
                  border: OutlineInputBorder(borderRadius:BorderRadius.circular(10),borderSide:const BorderSide(color:kBorder)),
                  enabledBorder: OutlineInputBorder(borderRadius:BorderRadius.circular(10),borderSide:const BorderSide(color:kBorder)),
                  contentPadding: const EdgeInsets.symmetric(horizontal:14,vertical:10),
                ),
                items: states.map((s)=>DropdownMenuItem(value:s,child:Text(s))).toList(),
                onChanged:(v){ setS((){selState=v??''; selCity='';});},
              ),
              const SizedBox(height:14),
              // City dropdown
              const Text("City", style:TextStyle(fontSize:13,fontWeight:FontWeight.w600,color:kMuted)),
              const SizedBox(height:6),
              DropdownButtonFormField<String>(
                value: selCity.isNotEmpty?selCity:null,
                hint: const Text("Select City"),
                isExpanded: true,
                decoration: InputDecoration(
                  border: OutlineInputBorder(borderRadius:BorderRadius.circular(10),borderSide:const BorderSide(color:kBorder)),
                  enabledBorder: OutlineInputBorder(borderRadius:BorderRadius.circular(10),borderSide:const BorderSide(color:kBorder)),
                  contentPadding: const EdgeInsets.symmetric(horizontal:14,vertical:10),
                ),
                items: cities.map((c)=>DropdownMenuItem(value:c,child:Text(c))).toList(),
                onChanged:(v){ setS((){selCity=v??'';});},
              ),
              const SizedBox(height:20),
              SizedBox(width:double.infinity, child:ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor:kPrimary, foregroundColor:Colors.white,
                  shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(12)),
                  padding:const EdgeInsets.symmetric(vertical:14),
                ),
                onPressed: selCity.isEmpty?null:() async {
                  Navigator.pop(ctx);
                  setState((){city=selCity; _cityManual=true;});
                  await Prefs.saveCity(selCity);
                  await Api.updateCity(widget.token, selCity);
                  await _fetchStores(selCity);
                },
                child: const Text("Apply", style:TextStyle(fontSize:15,fontWeight:FontWeight.w700)),
              )),
            ]),
          ),
        );
      }),
    );
  }

  Future<void> _initLoc() async {
    // If saved city exists, show stores immediately — no wait
    if (widget.savedCity.isNotEmpty) {
      setState(()=>city=widget.savedCity);
      _fetchStores(widget.savedCity);  // fire and forget — don't await here
    }
    // Detect GPS city + capture coordinates in parallel
    final detFuture = detectCity();
    try {
      final pos = await Geolocator.getCurrentPosition(desiredAccuracy:LocationAccuracy.medium)
          .timeout(const Duration(seconds:8));
      if(mounted) setState((){_userLat=pos.latitude; _userLng=pos.longitude;});
    } catch(_){}
    final det = await detFuture;
    if (!mounted) return;
    // If detection failed, check permission
    if (det == "Ballari" && widget.savedCity.isEmpty) {
      final perm = await Geolocator.checkPermission();
      final isDenied = perm == LocationPermission.denied || perm == LocationPermission.deniedForever;
      if (isDenied) {
        setState(() { city = ""; _loading = false; _locationDenied = true; });
        return;
      }
    }
    // Only re-fetch stores if detected city differs from what's already shown
    final detLower = det.toLowerCase().trim();
    final savedLower = widget.savedCity.toLowerCase().trim();
    if (detLower != savedLower) {
      setState(() { city = det; _locationDenied = false; });
      await Prefs.saveCity(det);
      Api.updateCity(widget.token, det);  // fire and forget
      await _fetchStores(det);
    } else {
      setState(() { city = det; _locationDenied = false; });
      Prefs.saveCity(det);
      Api.updateCity(widget.token, det);
    }
  }

  Future<void> _fetchStores(String c) async {
    if(mounted)setState((){_loading=true;_netError=false;_fetchFailed=false;});
    try {
      // Fetch categories and stores in parallel
      final results = await Future.wait([
        Api.fetchCategories(),
        Api.fetchStores(city:c,category:_cat=="All"?null:_cat),
      ]);
      final cats = results[0] as List<String>;
      final data = results[1] as List;
      if(!mounted)return;
      // Inject client-side distance + sort by proximity
      final List<Map<String,dynamic>> storeList = List<Map<String,dynamic>>.from(data);
      if(_userLat!=null && _userLng!=null){
        for(final s in storeList){
          final lat = double.tryParse(s["latitude"]?.toString()??"");
          final lng = double.tryParse(s["longitude"]?.toString()??"");
          if(lat!=null && lng!=null) s["distance_km"] = _haversineKm(_userLat!,_userLng!,lat,lng);
        }
        storeList.sort((a,b){
          final da=(a["distance_km"] as double?)??9999.0;
          final db2=(b["distance_km"] as double?)??9999.0;
          return da.compareTo(db2);
        });
      }
      setState((){_stores=storeList;_cats=["All",...cats];_loading=false;_netError=false;_fetchFailed=false;_page=0;_rebuildCaches();});
      _startSlide();
    } on SocketException catch(_){
      if(mounted)setState((){_loading=false;_netError=true;_fetchFailed=true;});
    } catch(_){
      if(mounted)setState((){_loading=false;_fetchFailed=true;});
    }
  }

  void _startSlide() {
    _slide?.cancel();
    _slide=Timer.periodic(const Duration(seconds:3),(_){
      if(_pc.hasClients&&_sl.isNotEmpty){
        final next=(_page+1)%_sl.length;
        _pc.animateToPage(next,duration:const Duration(milliseconds:500),curve:Curves.easeInOut);
      }
    });
  }



  void _showCatMenu() {
    setState(()=>_showCat=true); _catTimer?.cancel();
    _catTimer=Timer(const Duration(seconds:5),(){if(mounted)setState(()=>_showCat=false);});
  }

  @override void dispose() { _slide?.cancel();_catTimer?.cancel();_voucherTimer?.cancel();_sliderTimer?.cancel();_pc.dispose();_voucherPc.dispose();_sliderPc.dispose(); super.dispose(); }

  @override
  Widget build(BuildContext context) {
    final sl=_sl;
    final size = MediaQuery.of(context).size;
    final newStores = _newStores;
    final topStores = _topStores;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: const SystemUiOverlayStyle(
        statusBarColor: Colors.transparent,
        statusBarIconBrightness: Brightness.light,
        systemNavigationBarColor: Colors.white,
      ),
      child: Scaffold(
        extendBodyBehindAppBar: true,
        backgroundColor: kBg,
        body: _locationDenied
          ? _locationDeniedState()
          : Column(children: [

            // ══════ HEADER (dark green) ══════
            Container(
              color: kPrimary,
              child: SafeArea(
                bottom: false,
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(16,10,16,14),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    // ROW 1: Logo | City pill | Notif | Profile
                    Row(crossAxisAlignment: CrossAxisAlignment.center, children: [
                      buildImageLogo(height:30, white:true),
                      const SizedBox(width:10),
                      // City pill
                      GestureDetector(
                        onTap: ()=>_showCityPicker(context),
                        child: Container(
                          padding: const EdgeInsets.symmetric(horizontal:10,vertical:5),
                          decoration: BoxDecoration(
                            color: Colors.white.withOpacity(.15),
                            borderRadius: BorderRadius.circular(20),
                            border: Border.all(color:Colors.white24),
                          ),
                          child: Row(mainAxisSize:MainAxisSize.min, children:[
                            Icon(_cityManual?Icons.edit_location_alt_rounded:Icons.location_on_rounded, color:kLight, size:13),
                            const SizedBox(width:4),
                            Text(city.isNotEmpty?city:"Detecting...",
                              style:const TextStyle(color:Colors.white,fontSize:12,fontWeight:FontWeight.w700)),
                            const SizedBox(width:3),
                            const Icon(Icons.keyboard_arrow_down_rounded, color:Colors.white60, size:15),
                          ]),
                        ),
                      ),
                      const Spacer(),
                      // Profile avatar
                      GestureDetector(
                        onTap: ()=>_showProfile(context),
                        child: CircleAvatar(
                          radius:18,
                          backgroundColor:kAccent,
                          backgroundImage: (_profilePhoto!=null && _profilePhoto!.startsWith("data:image"))
                            ? MemoryImage(base64Decode(_profilePhoto!.split(",").last)) as ImageProvider
                            : (_profilePhoto!=null && _profilePhoto!.startsWith("http"))
                              ? NetworkImage(_profilePhoto!) as ImageProvider
                              : null,
                          child: (_profilePhoto==null||_profilePhoto!.isEmpty) ? Text(
                            widget.name.isNotEmpty?widget.name[0].toUpperCase():"U",
                            style:const TextStyle(color:Colors.white,fontWeight:FontWeight.bold,fontSize:14)) : null,
                        ),
                      ),
                    ]),

                    const SizedBox(height:14),

                    // ── Search bar (white) ──
                    GestureDetector(
                      onTap: ()=>Navigator.push(context,_route(_SearchPage(token:widget.token, city:city))),
                      child: Container(
                        height: 44,
                        decoration: BoxDecoration(
                          color: Colors.white,
                          borderRadius: BorderRadius.circular(12),
                        ),
                        child: Row(children:[
                          const SizedBox(width:14),
                          const Icon(Icons.search_rounded, color:kMuted, size:20),
                          const SizedBox(width:8),
                          const Expanded(child:Text("Search for stores, products...",
                            style:TextStyle(color:kMuted,fontSize:13))),
                          Padding(
                            padding:const EdgeInsets.only(right:10),
                            child:Icon(Icons.qr_code_scanner_rounded, color:kPrimary, size:20),
                          ),
                        ]),
                      ),
                    ),

                    const SizedBox(height:14),

                    // ── Category icons row ──
                    SizedBox(
                      height: 72,
                      child: ListView.builder(
                        scrollDirection: Axis.horizontal,
                        itemCount: _cats.length,
                        itemBuilder: (_, i) {
                          final c = _cats[i];
                          final bool sel = _cat == c;
                          final IconData icon = _categoryIcon(c);
                          return GestureDetector(
                            onTap:(){ setState((){_cat=c;_page=0;}); _fetchStores(city); },
                            child: Container(
                              margin: const EdgeInsets.only(right:16),
                              child: Column(mainAxisSize:MainAxisSize.min, children:[
                                Container(
                                  width:46, height:46,
                                  decoration: BoxDecoration(
                                    color: sel ? kPrimary : Colors.white,
                                    shape: BoxShape.circle,
                                    boxShadow:[BoxShadow(color:Colors.black.withOpacity(.08),blurRadius:6,offset:const Offset(0,2))],
                                  ),
                                  child: Icon(icon,
                                    color: sel ? Colors.white : kPrimary, size:22),
                                ),
                                const SizedBox(height:5),
                                Text(c,
                                  style:TextStyle(
                                    color: sel ? kLight : Colors.white,
                                    fontSize:11,
                                    fontWeight: sel ? FontWeight.w800 : FontWeight.w500)),
                              ]),
                            ),
                          );
                        },
                      ),
                    ),
                  ]),
                ),
              ),
            ),

            // ══════ BODY SCROLL + BOTTOM NAV ══════
            Expanded(child: Stack(children: [
              _loading
              ? _buildShimmerSkeleton()
              : sl.isEmpty
                ? _emptyState()
                : RefreshIndicator(
                  onRefresh: ()=>_fetchStores(city),
                  color: kPrimary,
                  child: CustomScrollView(slivers:[

                    // ── PROMO SLIDERS ──
                    if(_sliders.isNotEmpty)
                      SliverToBoxAdapter(child:Padding(
                        padding:const EdgeInsets.fromLTRB(14,14,14,0),
                        child:Column(children:[
                          SizedBox(
                            height: 160,
                            child: PageView.builder(
                              controller: _sliderPc,
                              itemCount: _sliders.length,
                              onPageChanged:(i)=>setState(()=>_sliderPage=i),
                              itemBuilder:(_,i)=>_PromoSliderCard(slider:Map<String,dynamic>.from(_sliders[i] as Map)),
                            ),
                          ),
                          const SizedBox(height:8),
                          // Dots
                          if(_sliders.length>1) Row(
                            mainAxisAlignment:MainAxisAlignment.center,
                            children: List.generate(_sliders.length,(i)=>AnimatedContainer(
                              duration:const Duration(milliseconds:250),
                              margin:const EdgeInsets.symmetric(horizontal:3),
                              width: _sliderPage==i ? 18 : 6,
                              height:6,
                              decoration:BoxDecoration(
                                color: _sliderPage==i ? kPrimary : kBorder,
                                borderRadius:BorderRadius.circular(3),
                              ),
                            )),
                          ),
                        ]),
                      )),

                    // ── NEW IN TOWN (NIT) ──
                    if(newStores.isNotEmpty)
                      SliverToBoxAdapter(child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
                        Padding(
                          padding:const EdgeInsets.fromLTRB(16,20,16,12),
                          child:Row(children:[
                            const Text("New in Town (NIT)",style:TextStyle(color:kText,fontSize:16,fontWeight:FontWeight.w900,letterSpacing:0.1)),
                            const Spacer(),
                            GestureDetector(
                              onTap:()=>_viewAll(context,"New In Town",newStores,bigCards:false),
                              child:const Text("View All",style:TextStyle(color:kPrimary,fontSize:13,fontWeight:FontWeight.w700)),
                            ),
                          ]),
                        ),
                        // NIT list — horizontal scroll cards
                        SizedBox(
                          height: 110,
                          child: ListView.builder(
                            scrollDirection: Axis.horizontal,
                            padding: const EdgeInsets.symmetric(horizontal:14),
                            itemCount: newStores.length,
                            itemBuilder:(_,i)=>GestureDetector(
                              onTap:()=>Navigator.push(context,_route(DetailPage(store:newStores[i],token:widget.token))),
                              child:_NitHorizontalCard(store:newStores[i]),
                            ),
                          ),
                        ),
                        const SizedBox(height:4),
                      ])),

                    // ── VOUCHER ZONE ──
                    if(_vouchers.isNotEmpty)
                      SliverToBoxAdapter(child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
                        Padding(
                          padding:const EdgeInsets.fromLTRB(16,20,16,12),
                          child:Row(children:[
                            const Text("Voucher Zone",style:TextStyle(color:kText,fontSize:16,fontWeight:FontWeight.w900)),
                            const Spacer(),
                            GestureDetector(
                              onTap:()=>_viewAllVouchers(context),
                              child:const Text("View All",style:TextStyle(color:kPrimary,fontSize:13,fontWeight:FontWeight.w700)),
                            ),
                          ]),
                        ),
                        // 2-column horizontal voucher cards (show 2 at a time)
                        SizedBox(
                          height: 130,
                          child: ListView.builder(
                            scrollDirection: Axis.horizontal,
                            padding: const EdgeInsets.symmetric(horizontal:14),
                            itemCount: _vouchers.length,
                            itemBuilder:(_,i)=>SizedBox(
                              width: MediaQuery.of(context).size.width * 0.72,
                              child: _GiftVoucherCard(voucher:Map<String,dynamic>.from(_vouchers[i] as Map)),
                            ),
                          ),
                        ),
                        const SizedBox(height:8),
                      ])),

                    // ── STORES NEAR YOU ──
                    if(topStores.isNotEmpty)
                      SliverToBoxAdapter(child:Padding(
                        padding:const EdgeInsets.fromLTRB(16,20,16,12),
                        child:Row(children:[
                          const Text("Stores Near You",style:TextStyle(color:kText,fontSize:16,fontWeight:FontWeight.w900)),
                          const Spacer(),
                          GestureDetector(
                            onTap:()=>_viewAll(context,"Stores Near You",topStores),
                            child:const Text("View All",style:TextStyle(color:kPrimary,fontSize:13,fontWeight:FontWeight.w700)),
                          ),
                        ]),
                      )),

                    if(topStores.isNotEmpty)
                      SliverPadding(
                        padding:const EdgeInsets.fromLTRB(14,0,14,0),
                        sliver:SliverList(
                          delegate:SliverChildBuilderDelegate(
                            (ctx,i)=>Padding(
                              padding:const EdgeInsets.only(bottom:10),
                              child:GestureDetector(
                                onTap:()=>Navigator.push(context,_route(DetailPage(store:topStores[i],token:widget.token))),
                                child:_TopStoreCard(store:topStores[i]),
                              ),
                            ),
                            childCount:topStores.length,
                          ),
                        ),
                      ),

                    // fallback if no stores at all
                    if(newStores.isEmpty && topStores.isEmpty && sl.isNotEmpty)
                      SliverToBoxAdapter(child:Padding(
                        padding:const EdgeInsets.fromLTRB(16,20,16,12),
                        child:Row(children:[
                          const Text("Stores Near You",style:TextStyle(color:kText,fontSize:16,fontWeight:FontWeight.w900)),
                          const Spacer(),
                          GestureDetector(
                            onTap:()=>_viewAll(context,"Stores Near You",sl),
                            child:const Text("View All",style:TextStyle(color:kPrimary,fontSize:13,fontWeight:FontWeight.w700)),
                          ),
                        ]),
                      )),
                    if(newStores.isEmpty && topStores.isEmpty)
                      SliverPadding(
                        padding:const EdgeInsets.fromLTRB(14,0,14,0),
                        sliver:SliverList(
                          delegate:SliverChildBuilderDelegate(
                            (ctx,i)=>Padding(
                              padding:const EdgeInsets.only(bottom:10),
                              child:GestureDetector(
                                onTap:()=>Navigator.push(context,_route(DetailPage(store:sl[i],token:widget.token))),
                                child:_TopStoreCard(store:sl[i]),
                              ),
                            ),
                            childCount:sl.length,
                          ),
                        ),
                      ),

                    const SliverToBoxAdapter(child:SizedBox(height:100)),
                  ]),
                ),

            // ── BOTTOM NAV ──
            Positioned(
              bottom: 0, left: 0, right: 0,
              child: Container(
                height: 72,
                decoration: BoxDecoration(
                  color: Colors.white,
                  boxShadow: [BoxShadow(color:Colors.black.withOpacity(.08),blurRadius:16,offset:const Offset(0,-2))],
                ),
                child: Row(mainAxisAlignment:MainAxisAlignment.spaceEvenly, children: [
                  // Home
                  GestureDetector(
                    onTap:(){ setState(()=>_navIdx=0); },
                    child:_NavBtn(icon:Icons.home_rounded,label:"Home",active:_navIdx==0),
                  ),
                  // QR Scanner — round highlight
                  GestureDetector(
                    onTap:()=>Navigator.push(context,_route(QRPage(token:widget.token,onDone:_loadWallet))),
                    child: Container(
                      width:58, height:58,
                      decoration:BoxDecoration(
                        color:kPrimary,
                        shape:BoxShape.circle,
                        boxShadow:[BoxShadow(color:kPrimary.withOpacity(.4),blurRadius:14,offset:const Offset(0,4))],
                      ),
                      child:const Icon(Icons.qr_code_scanner_rounded,color:Colors.white,size:28),
                    ),
                  ),
                  // Search
                  GestureDetector(
                    onTap:(){ setState(()=>_navIdx=2); _searchStores(context); },
                    child:_NavBtn(icon:Icons.search_rounded,label:"Search",active:_navIdx==2),
                  ),
                ]),
              ),
            ),
          ])),
        ]),
      ),
    );
  }

  Widget _storeCard(Map store, VoidCallback onTap) {
    // kept for compatibility — actual rendering now in _StackedCards
    return GestureDetector(onTap:onTap,child:_buildCardContent(store));
  }

  Widget _buildCardContent(Map s){
    final img=s["image"];
    if(img!=null&&img.toString().startsWith("data:image")){
      try{ return Image.memory(base64Decode(img.toString().split(",").last),fit:BoxFit.cover,gaplessPlayback:true); }
      catch(_){}
    }
    return _placeholder(s);
  }

  Widget _placeholder(Map s)=>Container(
    decoration:BoxDecoration(
      gradient:LinearGradient(colors:[kPrimary,const Color(0xFF2a4a40)],begin:Alignment.topLeft,end:Alignment.bottomRight)),
    child:Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
      const Icon(Icons.store_mall_directory_outlined,size:80,color:kLight),const SizedBox(height:14),
      Text(s["store_name"]??"",style:const TextStyle(color:Colors.white,fontSize:20,fontWeight:FontWeight.bold),textAlign:TextAlign.center),
      const SizedBox(height:4),
      Text(s["city"]??"",style:const TextStyle(color:kAccent,fontSize:13)),
    ])));

  Widget _locationDeniedState() => Container(
    color: kPrimary,
    child: Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
      const Icon(Icons.location_off, color: kLight, size: 64),
      const SizedBox(height: 20),
      const Text("Location Access Required", style: TextStyle(color: Colors.white, fontSize: 20, fontWeight: FontWeight.bold)),
      const SizedBox(height: 12),
      const Padding(padding: EdgeInsets.symmetric(horizontal: 40),
        child: Text("Please allow location access to find stores near you.", style: TextStyle(color: kAccent, fontSize: 14), textAlign: TextAlign.center)),
      const SizedBox(height: 28),
      ElevatedButton.icon(
        style: ElevatedButton.styleFrom(backgroundColor: kLight, foregroundColor: kPrimary, padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 14), shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(24))),
        icon: const Icon(Icons.settings),
        label: const Text("Open Settings", style: TextStyle(fontWeight: FontWeight.bold)),
        onPressed: () => Geolocator.openAppSettings(),
      ),
      const SizedBox(height: 12),
      TextButton(
        onPressed: () async { setState(()=>_locationDenied=false); await _initLoc(); },
        child: const Text("Try Again", style: TextStyle(color: Colors.white70)),
      ),
    ])),
  );

  Widget _buildShimmerSkeleton() => Shimmer.fromColors(
    baseColor: const Color(0xFFE8F0ED),
    highlightColor: const Color(0xFFF5FAF7),
    child: ListView.builder(
      padding: const EdgeInsets.symmetric(horizontal:18, vertical:14),
      itemCount: 5,
      itemBuilder: (_,__) => Container(
        margin: const EdgeInsets.only(bottom:18),
        height: 180,
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(18),
        ),
      ),
    ),
  );

  Widget _emptyState()=>Container(
    color:kPrimary,
    child:Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
      buildLogo(44,kLight),const SizedBox(height:16),
      if(_netError)...[
        const Icon(Icons.wifi_off_rounded,color:Colors.white54,size:40),
        const SizedBox(height:10),
        const Text("No internet connection",style:TextStyle(color:Colors.white,fontSize:16,fontWeight:FontWeight.w700)),
        const SizedBox(height:6),
        const Text("Check your connection and try again",style:TextStyle(color:Colors.white60,fontSize:13)),
      ] else if(_fetchFailed)...[
        const Icon(Icons.cloud_off_rounded,color:Colors.white54,size:40),
        const SizedBox(height:10),
        const Text("Couldn\'t load stores",style:TextStyle(color:Colors.white,fontSize:16,fontWeight:FontWeight.w700)),
        const SizedBox(height:6),
        const Text("Please try again",style:TextStyle(color:Colors.white60,fontSize:13)),
      ] else...[
        Text("No stores in \$city yet",style:const TextStyle(color:kLight,fontSize:16)),
      ],
      const SizedBox(height:14),
      TextButton(onPressed:()=>_fetchStores(city),child:const Text("Refresh",style:TextStyle(color:Colors.white))),
    ])));

  Widget _actionBtn(String label,VoidCallback onTap)=>ElevatedButton(
    style:ElevatedButton.styleFrom(backgroundColor:Colors.white,foregroundColor:kPrimary,minimumSize:const Size(120,40),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(22)),elevation:4),
    onPressed:onTap,child:Text(label,style:const TextStyle(fontWeight:FontWeight.bold,fontSize:12)));

  void _searchStores(BuildContext ctx) {
    Navigator.push(ctx, _route(_SearchPage(token:widget.token, city:city)));
  }

  // Profile sheet (replaces _more) — all options inside
  void _showProfile(BuildContext ctx) => showModalBottomSheet(
    context:ctx,
    isScrollControlled:true,
    backgroundColor:Colors.transparent,
    builder:(ctx2)=>DraggableScrollableSheet(
      initialChildSize:0.72,
      minChildSize:0.45,
      maxChildSize:0.92,
      expand:false,
      builder:(_,sc)=>Container(
        decoration:const BoxDecoration(color:Colors.white,borderRadius:BorderRadius.vertical(top:Radius.circular(24))),
        child:Column(children:[
          Container(width:40,height:4,margin:const EdgeInsets.only(top:12,bottom:4),
            decoration:BoxDecoration(color:Colors.grey.shade300,borderRadius:BorderRadius.circular(2))),
          _UserProfileHeader(token:widget.token,name:widget.name,phone:widget.phone),
          const Divider(height:1),
          Expanded(child:ListView(controller:sc,children:[
            _pItem(ctx,Icons.search_rounded,"Search Stores",()=>_searchStores(ctx)),
            _pItem(ctx,Icons.account_balance_wallet_rounded,"My Wallet",()=>Navigator.push(ctx,_route(WalletPage(token:widget.token)))),
            _pItem(ctx,Icons.history_rounded,"Scan History",()=>Navigator.push(ctx,_route(HistoryPage(token:widget.token)))),
            _pItem(ctx,Icons.favorite_rounded,"My Favourites",()=>Navigator.push(ctx,_route(FavoritesPage(token:widget.token)))),
            const Divider(height:1),
            _pItem(ctx,Icons.info_outline_rounded,"About Us",()async{final c=await Api.getAboutUs();if(!ctx.mounted)return;showDialog(context:ctx,builder:(_)=>_PolicyDialog(title:"About Us",body:c.isEmpty?"Offro connects local stores with customers through deals and loyalty points.":c));}),
            _pItem(ctx,Icons.description_rounded,"Terms & Conditions",()async{final c=await Api.fetchTerms("user");if(!ctx.mounted)return;showDialog(context:ctx,builder:(_)=>_PolicyDialog(title:"Terms & Conditions",body:c));}),
            _pItem(ctx,Icons.privacy_tip_rounded,"Privacy Policy",()async{final c=await Api.fetchPolicy("privacy");if(!ctx.mounted)return;showDialog(context:ctx,builder:(_)=>_PolicyDialog(title:"Privacy Policy",body:c));}),
            _pItem(ctx,Icons.receipt_rounded,"Refund Policy",()async{final c=await Api.fetchPolicy("refund");if(!ctx.mounted)return;showDialog(context:ctx,builder:(_)=>_PolicyDialog(title:"Refund Policy",body:c));}),
            const Divider(height:1),
            _pItem(ctx,Icons.chat_bubble_rounded,"Contact Offro",()async{
              final s=await Api.getSocialLinks();
              final rawWa=s["whatsapp"]??"";
              if(rawWa.isNotEmpty){
                final digits=rawWa.replaceAll(RegExp(r'[^0-9]'),'');
                final waNum=digits.length>=10?(digits.length==10?"91$digits":digits):digits;
                await launchUrl(Uri.parse("https://wa.me/$waNum"),mode:LaunchMode.externalApplication);
              }
            },color:const Color(0xFF25D366)),
            _pItem(ctx,Icons.logout_rounded,"Logout",()async{await Prefs.clear();if(!ctx.mounted)return;Navigator.pushAndRemoveUntil(ctx,_route(const OnboardingScreen()),(_)=>false);},color:Colors.red),
            const SizedBox(height:28),
          ])),
        ]),
      ),
    ));

  Widget _pItem(BuildContext ctx,IconData icon,String title,VoidCallback onTap,{Color color=kPrimary})=>
    ListTile(
      leading:Container(
        width:38,height:38,
        decoration:BoxDecoration(
          color:color==kPrimary?kLight:color.withOpacity(.1),
          borderRadius:BorderRadius.circular(10)),
        child:Icon(icon,color:color==kPrimary?kPrimary:color,size:18)),
      title:Text(title,style:TextStyle(color:color==kPrimary?kText:color,fontWeight:FontWeight.w600,fontSize:14)),
      trailing:color==kPrimary?const Icon(Icons.arrow_forward_ios_rounded,size:13,color:kMuted):null,
      onTap:(){ Navigator.pop(ctx); onTap(); });

  // View All page
  void _viewAllVouchers(BuildContext ctx) =>
    Navigator.push(ctx, _route(_VoucherViewAllPage(vouchers: _vouchers)));

  void _viewAll(BuildContext ctx, String title, List<Map<String,dynamic>> stores, {bool bigCards=false}) =>
    Navigator.push(ctx, _route(_ViewAllPage(title:title, stores:stores, token:widget.token, bigCards:bigCards)));
}

// ─────────────────────── VIEW ALL PAGE ───────────────────────

// ─────────────────────── SEARCH PAGE ───────────────────────
class _SearchPage extends StatefulWidget {
  final String token; final String city;
  const _SearchPage({required this.token, required this.city});
  @override State<_SearchPage> createState()=>_SearchPageState();
}
class _SearchPageState extends State<_SearchPage> {
  final _sc = TextEditingController();
  String _q = ""; bool _busy = false;
  List<Map<String,dynamic>> _results = [];

  @override void dispose(){ _sc.dispose(); super.dispose(); }

  Future<void> _doSearch(String q) async {
    final qt = q.trim();
    if (qt.isEmpty) { setState(()=>_results=[]); return; }
    setState(()=>_busy=true);
    try {
      final all = await Api.fetchStores(city:widget.city);
      final ql = qt.toLowerCase();
      _results = List<Map<String,dynamic>>.from(all.where((s)=>
        (s["store_name"]??'').toLowerCase().contains(ql)||
        (s["category"]??'').toLowerCase().contains(ql)||
        (s["area"]??'').toLowerCase().contains(ql)||
        (s["offer"]??'').toLowerCase().contains(ql)
      ).toList());
    } catch(_) { _results = []; }
    if (mounted) setState(()=>_busy=false);
  }

  @override Widget build(BuildContext context){
    return Scaffold(
      backgroundColor: kBg,
      appBar: AppBar(
        backgroundColor: kPrimary,
        foregroundColor: Colors.white,
        titleSpacing: 0,
        title: TextField(
          controller: _sc,
          autofocus: true,
          style: const TextStyle(color:Colors.white, fontSize:15),
          cursorColor: Colors.white,
          decoration: InputDecoration(
            hintText: "Search stores in ${widget.city}...",
            hintStyle: const TextStyle(color:Colors.white60, fontSize:14),
            border: InputBorder.none,
            contentPadding: const EdgeInsets.symmetric(vertical:14),
          ),
          onChanged:(v){ setState(()=>_q=v); if(v.trim().isNotEmpty) _doSearch(v); else setState(()=>_results=[]); },
          onSubmitted:_doSearch,
        ),
        actions:[
          if (_q.isNotEmpty) IconButton(icon:const Icon(Icons.clear,color:Colors.white),onPressed:(){ _sc.clear(); setState((){ _q=''; _results=[]; }); }),
        ],
      ),
      body: _busy
        ? const Center(child:CircularProgressIndicator(color:kPrimary))
        : _q.isEmpty
          ? _SearchLandingGrid(token:widget.token, city:widget.city)
          : _results.isEmpty
            ? Center(child:Column(mainAxisSize:MainAxisSize.min,children:[
                const Icon(Icons.search_off,color:kAccent,size:56),
                const SizedBox(height:12),
                Text("No results for '$_q'",style:const TextStyle(color:kMuted,fontSize:15)),
              ]))
            : ListView.separated(
                padding:const EdgeInsets.all(14),
                separatorBuilder:(_,__)=>const SizedBox(height:8),
                itemCount:_results.length,
                itemBuilder:(_,i){
                  final s=_results[i];
                  final img=s["image"]?.toString()??'';
                  return GestureDetector(
                    onTap:()=>Navigator.push(context,_route(DetailPage(store:s,token:widget.token))),
                    child:Container(padding:const EdgeInsets.all(12),
                      decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(14),
                        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.05),blurRadius:8,offset:const Offset(0,2))]),
                      child:Row(children:[
                        ClipRRect(borderRadius:BorderRadius.circular(10),child:SizedBox(width:62,height:62,
                          child:img.isNotEmpty&&img.startsWith("data:image")
                            ? Image.memory(base64Decode(img.split(",").last),fit:BoxFit.cover)
                            : Container(color:kAccent,child:const Icon(Icons.store,color:kPrimary,size:28)))),
                        const SizedBox(width:12),
                        Expanded(child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
                          Text(s["store_name"]??"",style:const TextStyle(fontWeight:FontWeight.bold,color:kText,fontSize:14)),
                          Text("${s['category']??''} · ${s['area']??''}",style:const TextStyle(color:kMuted,fontSize:12)),
                          if ((s['visit_points']??0)>0) Padding(padding:const EdgeInsets.only(top:3),
                            child:Text("${s['visit_points']} pts on visit",style:const TextStyle(color:kPrimary,fontSize:11,fontWeight:FontWeight.w600))),
                        ])),
                        const Icon(Icons.arrow_forward_ios_rounded,color:kBorder,size:14),
                      ])));
                }),
    );
  }
}

// ─────────────────────── SEARCH LANDING GRID ───────────────────────
class _SearchLandingGrid extends StatefulWidget {
  final String token; final String city;
  const _SearchLandingGrid({required this.token,required this.city});
  @override State<_SearchLandingGrid> createState()=>_SearchLandingGridState();
}
class _SearchLandingGridState extends State<_SearchLandingGrid> {
  List<Map<String,dynamic>> _stores=[];bool _loading=true;
  @override void initState(){super.initState();_load();}
  Future<void> _load() async {
    try{final d=await Api.fetchStores(city:widget.city);if(mounted)setState((){_stores=List<Map<String,dynamic>>.from(d);_loading=false;});}
    catch(_){if(mounted)setState(()=>_loading=false);}
  }
  @override Widget build(BuildContext context){
    if(_loading) return const Center(child:CircularProgressIndicator(color:kPrimary));
    if(_stores.isEmpty) return const Center(child:Text("No stores yet",style:TextStyle(color:kMuted)));
    return _MasonrySearchGrid(stores:_stores, token:widget.token);
  }
}

class _ViewAllPage extends StatefulWidget {
  final String title;
  final List<Map<String,dynamic>> stores;
  final String token;
  final bool bigCards; // true = New In Town style, false = grid
  const _ViewAllPage({required this.title, required this.stores, required this.token, this.bigCards=false});
  @override State<_ViewAllPage> createState()=>_ViewAllPageState();
}
class _ViewAllPageState extends State<_ViewAllPage>{
  final _searchCtrl = TextEditingController();
  String _q = "";
  @override void dispose(){ _searchCtrl.dispose(); super.dispose(); }

  List<Map<String,dynamic>> get _filtered {
    if (_q.trim().isEmpty) return widget.stores;
    final q = _q.toLowerCase();
    return widget.stores.where((s){
      return (s["store_name"]??"").toString().toLowerCase().contains(q)
          || (s["area"]??"").toString().toLowerCase().contains(q)
          || (s["city"]??"").toString().toLowerCase().contains(q)
          || (s["category"]??"").toString().toLowerCase().contains(q)
          || (s["offer"]??"").toString().toLowerCase().contains(q)
          || (s["about"]??"").toString().toLowerCase().contains(q);
    }).toList();
  }

  @override Widget build(BuildContext context){
    final filtered = _filtered;
    final scrH = MediaQuery.of(context).size.height;
    return Scaffold(
      backgroundColor: kBg,
      appBar: AppBar(
        backgroundColor: kPrimary,
        foregroundColor: Colors.white,
        title: Text(widget.title, style:const TextStyle(fontWeight:FontWeight.w800)),
        elevation: 0,
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(54),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(14,0,14,10),
            child: Container(
              decoration: BoxDecoration(color:Colors.white, borderRadius:BorderRadius.circular(12)),
              child: TextField(
                controller: _searchCtrl,
                onChanged: (v)=>setState(()=>_q=v),
                style: const TextStyle(color:kText, fontSize:14),
                decoration: InputDecoration(
                  hintText: "Search store, area, offer...",
                  hintStyle: const TextStyle(color:kMuted, fontSize:13),
                  prefixIcon: const Icon(Icons.search, color:kMuted, size:20),
                  suffixIcon: _q.isNotEmpty ? IconButton(icon:const Icon(Icons.clear,size:18,color:kMuted), onPressed:(){_searchCtrl.clear();setState(()=>_q="");}) : null,
                  border: InputBorder.none,
                  contentPadding: const EdgeInsets.symmetric(vertical:12),
                ),
              ),
            ),
          ),
        ),
      ),
      body: filtered.isEmpty
        ? Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
            const Icon(Icons.search_off,color:kAccent,size:56),const SizedBox(height:12),
            Text(_q.isEmpty?"No stores yet":"No results for '$_q'",style:const TextStyle(color:kMuted,fontSize:15))]))
        : ListView.builder(
            padding:const EdgeInsets.fromLTRB(14,8,14,24),
            itemCount:filtered.length,
            itemBuilder:(_,i)=>Padding(
              padding:const EdgeInsets.only(bottom:10),
              child:GestureDetector(
                onTap:()=>Navigator.push(context,_route(DetailPage(store:filtered[i],token:widget.token))),
                child:_TopStoreCard(store:filtered[i]),
              ),
            )),
    );
  }
}

// ─────────────────────── FAVORITES PAGE ───────────────────────
class FavoritesPage extends StatefulWidget {
  final String token;
  const FavoritesPage({super.key,required this.token});
  @override State<FavoritesPage> createState()=>_FavoritesState();
}
class _FavoritesState extends State<FavoritesPage>{
  List _favs=[]; bool _loading=true;
  @override void initState(){super.initState();_load();}
  Future<void> _load() async {
    try{final d=await Api.getFavorites(widget.token);if(mounted)setState((){_favs=d;_loading=false;});}
    catch(_){if(mounted)setState(()=>_loading=false);}
  }
  @override Widget build(BuildContext ctx)=>Scaffold(
    backgroundColor:kBg,
    appBar:AppBar(backgroundColor:kPrimary,foregroundColor:Colors.white,title:const Text("My Favourites",style:TextStyle(fontWeight:FontWeight.w800)),elevation:0),
    body:_loading?const Center(child:CircularProgressIndicator(color:kPrimary)):_favs.isEmpty
      ?Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
          const Icon(Icons.favorite_border,color:kAccent,size:64),const SizedBox(height:16),
          const Text("No favourites yet",style:TextStyle(color:kMuted,fontSize:16)),
        ]))
      :GridView.builder(
        padding:const EdgeInsets.all(14),
        gridDelegate:const SliverGridDelegateWithFixedCrossAxisCount(crossAxisCount:2,crossAxisSpacing:10,mainAxisSpacing:10,childAspectRatio:0.82),
        itemCount:_favs.length,
        itemBuilder:(_,i)=>GestureDetector(
          onTap:()=>Navigator.push(ctx,_route(DetailPage(store:Map<String,dynamic>.from(_favs[i] as Map),token:widget.token))),
          child:_GridStoreCard(store:Map<String,dynamic>.from(_favs[i] as Map))),
      ),
  );
}

// ─────────────────────── PROMO SLIDER CARD ───────────────────────
class _PromoSliderCard extends StatelessWidget {
  final Map<String,dynamic> slider;
  const _PromoSliderCard({required this.slider});

  @override Widget build(BuildContext context) {
    final title    = slider["title"]?.toString() ?? "Exclusive Offer";
    final subtitle = slider["subtitle"]?.toString() ?? slider["text"]?.toString() ?? "";
    final code     = slider["code"]?.toString() ?? slider["promo_code"]?.toString() ?? "";
    final imgUrl   = slider["image"]?.toString() ?? slider["img"]?.toString() ?? "";
    final bgColor  = slider["bg_color"] != null
        ? Color(int.tryParse(slider["bg_color"].toString().replaceAll("#","0xFF")) ?? 0xFF3E5F55)
        : kPrimary;

    return Container(
      margin: const EdgeInsets.symmetric(horizontal:4),
      decoration: BoxDecoration(
        color: bgColor,
        borderRadius: BorderRadius.circular(18),
        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.12),blurRadius:14,offset:const Offset(0,5))],
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(18),
        child: Stack(children:[
          // Background image (right side)
          if(imgUrl.isNotEmpty)
            Positioned(right:0,top:0,bottom:0,
              child: SizedBox(
                width: 160,
                child: imgUrl.startsWith("data:image")
                  ? Image.memory(base64Decode(imgUrl.split(",").last), fit:BoxFit.cover, gaplessPlayback:true)
                  : CachedNetworkImage(imageUrl:imgUrl, fit:BoxFit.cover,
                      errorWidget:(_,__,___)=>const SizedBox()),
              ),
            ),
          // Gradient overlay
          Positioned.fill(child:Container(
            decoration: BoxDecoration(
              gradient: LinearGradient(
                colors:[bgColor, bgColor.withOpacity(.7), Colors.transparent],
                stops: const [0.0, 0.55, 1.0],
                begin: Alignment.centerLeft, end: Alignment.centerRight,
              ),
            ),
          )),
          // Text content
          Padding(
            padding: const EdgeInsets.fromLTRB(20,18,120,18),
            child: Column(crossAxisAlignment:CrossAxisAlignment.start, mainAxisAlignment:MainAxisAlignment.center, children:[
              Text(title,
                style: const TextStyle(color:Colors.white, fontSize:22, fontWeight:FontWeight.w900, height:1.15),
                maxLines:2, overflow:TextOverflow.ellipsis),
              if(subtitle.isNotEmpty) Padding(
                padding:const EdgeInsets.only(top:4),
                child:Text(subtitle,style:const TextStyle(color:Colors.white70,fontSize:12,fontWeight:FontWeight.w500),maxLines:2),
              ),
              if(code.isNotEmpty) Padding(
                padding:const EdgeInsets.only(top:10),
                child:Container(
                  padding:const EdgeInsets.symmetric(horizontal:12,vertical:5),
                  decoration:BoxDecoration(
                    color:Colors.white,
                    borderRadius:BorderRadius.circular(8),
                  ),
                  child:Text("Use Code: $code",
                    style:TextStyle(color:bgColor,fontSize:12,fontWeight:FontWeight.w900,letterSpacing:0.5)),
                ),
              ),
              Padding(
                padding:const EdgeInsets.only(top:6),
                child:const Text("T&C Apply",style:TextStyle(color:Colors.white38,fontSize:10)),
              ),
            ]),
          ),
        ]),
      ),
    );
  }
}

// ─────────────────────── NIT HORIZONTAL CARD ───────────────────────
class _NitHorizontalCard extends StatelessWidget {
  final Map<String,dynamic> store;
  const _NitHorizontalCard({required this.store});

  Widget _img(String? url, String name) {
    if (url == null || url.isEmpty) return _fallback(name);
    if (url.startsWith("data:image")) {
      try { return Image.memory(base64Decode(url.split(",").last),fit:BoxFit.cover,width:double.infinity,height:double.infinity,gaplessPlayback:true); } catch(_){}
    }
    if (url.startsWith("http")) {
      return CachedNetworkImage(imageUrl:url,fit:BoxFit.cover,width:double.infinity,height:double.infinity,
        placeholder:(_,__)=>_fallback(name), errorWidget:(_,__,___)=>_fallback(name));
    }
    return _fallback(name);
  }
  Widget _fallback(String n) => Container(color:kAccent,child:Center(child:Text(n.isNotEmpty?n[0]:"S",style:const TextStyle(color:kPrimary,fontWeight:FontWeight.w900,fontSize:18))));

  @override Widget build(BuildContext context) {
    final name     = store["store_name"]?.toString() ?? "";
    final category = store["category"]?.toString() ?? "";
    final imgUrl   = store["image"]?.toString() ?? "";
    final double? distKm = (store["distance_km"] as num?)?.toDouble();

    return Container(
      width: 140,
      margin: const EdgeInsets.only(right:12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.07),blurRadius:8,offset:const Offset(0,3))],
      ),
      child: Column(crossAxisAlignment:CrossAxisAlignment.start, children:[
        // Image
        Stack(children:[
          ClipRRect(
            borderRadius: const BorderRadius.vertical(top:Radius.circular(14)),
            child: SizedBox(width:double.infinity, height:68, child:_img(imgUrl.isNotEmpty?imgUrl:null, name)),
          ),
          // "New" badge
          Positioned(top:6,left:6,
            child:Container(
              padding:const EdgeInsets.symmetric(horizontal:6,vertical:2),
              decoration:BoxDecoration(color:kPrimary,borderRadius:BorderRadius.circular(6)),
              child:const Text("New",style:TextStyle(color:Colors.white,fontSize:9,fontWeight:FontWeight.w800)),
            )),
        ]),
        // Info
        Padding(
          padding:const EdgeInsets.fromLTRB(8,6,8,6),
          child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
            Text(name,style:const TextStyle(color:kText,fontSize:12,fontWeight:FontWeight.w800),maxLines:1,overflow:TextOverflow.ellipsis),
            Text(category,style:const TextStyle(color:kMuted,fontSize:10,fontWeight:FontWeight.w500)),
            if(distKm!=null) Row(children:[
              const Icon(Icons.location_on_rounded,color:kMuted,size:10),const SizedBox(width:2),
              Text(distKm<1?"${(distKm*1000).round()}m":"${distKm.toStringAsFixed(1)} km",
                style:const TextStyle(color:kMuted,fontSize:10)),
            ]),
          ]),
        ),
      ]),
    );
  }
}

// ─────────────────────── VOUCHER VIEW ALL PAGE ───────────────────────
class _VoucherViewAllPage extends StatefulWidget {
  final List<Map<String,dynamic>> vouchers;
  const _VoucherViewAllPage({required this.vouchers});
  @override State<_VoucherViewAllPage> createState()=>_VoucherViewAllPageState();
}
class _VoucherViewAllPageState extends State<_VoucherViewAllPage>{
  String _cat = "All";
  List<String> get _cats {
    final s = {"All"};
    for(final v in widget.vouchers){
      final c = v["category"]?.toString() ?? "";
      if(c.isNotEmpty) s.add(c);
    }
    return s.toList();
  }
  List<Map<String,dynamic>> get _filtered => _cat=="All"
      ? widget.vouchers
      : widget.vouchers.where((v)=>(v["category"]?.toString()??"")==_cat).toList();

  @override Widget build(BuildContext context){
    return Scaffold(
      backgroundColor: kBg,
      appBar: AppBar(
        backgroundColor: kPrimary,
        foregroundColor: Colors.white,
        title: const Text("Voucher Zone", style:TextStyle(fontWeight:FontWeight.w800)),
        elevation: 0,
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(50),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(14,0,14,10),
            child: SizedBox(
              height: 36,
              child: ListView(
                scrollDirection: Axis.horizontal,
                children: _cats.map((c)=>GestureDetector(
                  onTap:()=>setState(()=>_cat=c),
                  child:Container(
                    margin:const EdgeInsets.only(right:8),
                    padding:const EdgeInsets.symmetric(horizontal:14,vertical:6),
                    decoration:BoxDecoration(
                      color: _cat==c ? kLight : Colors.white.withOpacity(.2),
                      borderRadius:BorderRadius.circular(20),
                    ),
                    child:Text(c,style:TextStyle(color:_cat==c?kPrimary:Colors.white,fontSize:12,fontWeight:FontWeight.w700)),
                  ),
                )).toList(),
              ),
            ),
          ),
        ),
      ),
      body: _filtered.isEmpty
        ? const Center(child:Text("No vouchers available",style:TextStyle(color:kMuted)))
        : ListView.builder(
            padding: const EdgeInsets.fromLTRB(0,12,0,24),
            itemCount: _filtered.length,
            itemBuilder:(_,i)=>_GiftVoucherCard(voucher:Map<String,dynamic>.from(_filtered[i])),
          ),
    );
  }
}

// ─────────────────────── DETAIL PAGE ───────────────────────
class DetailPage extends StatefulWidget {
  final Map store; final String token;
  const DetailPage({super.key,required this.store,required this.token});
  @override State<DetailPage> createState() => _DetailPageState();
}
class _DetailPageState extends State<DetailPage> with SingleTickerProviderStateMixin {
  Map<String,dynamic> _store = {};
  bool _loadingDetail = true;
  bool _isFav = false;
  double? _myRating;
  bool _ratingSubmitting = false;
  int _imgPage = 0;
  late TabController _tabCtrl;
  final PageController _imgPc = PageController();
  Timer? _autoScroll;

  @override void initState() {
    super.initState();
    _tabCtrl = TabController(length:3, vsync:this);
    _store = Map<String,dynamic>.from(widget.store);
    _fetchFullStore();
  }

  void _startAutoScroll(int imgCount) {
    _autoScroll?.cancel();
    if (imgCount < 2) return;
    _autoScroll = Timer.periodic(const Duration(seconds:3), (_) {
      if (!mounted) return;
      final next = (_imgPage + 1) % imgCount;
      _imgPc.animateToPage(next, duration:const Duration(milliseconds:400), curve:Curves.easeInOut);
    });
  }

  @override void dispose(){ _tabCtrl.dispose(); _imgPc.dispose(); _autoScroll?.cancel(); super.dispose(); }

  Future<void> _fetchFullStore() async {
    try {
      final id = widget.store["_id"]?.toString() ?? "";
      if (id.isEmpty) { setState(()=>_loadingDetail=false); return; }
      final full = await Api.fetchStoreDetail(id);
      final fav  = await Api.isFavorite(widget.token, id);
      final myR  = await Api.getUserRating(widget.token, id);
      final imgs2 = (full["images"] as List?)?.map((x)=>x.toString()).toList() ?? [];
      final mainImg2 = full["image"]?.toString() ?? "";
      final allImgs2 = [if(mainImg2.isNotEmpty) mainImg2, ...imgs2];
      if (mounted) setState(() {
        // Preserve client-computed distance_km (backend doesn't return it)
        final savedDist = _store["distance_km"];
        _store = Map<String,dynamic>.from(full);
        if (savedDist != null) _store["distance_km"] = savedDist;
        _isFav = fav;
        _myRating = (myR?["rating"] as num?)?.toDouble();
        _loadingDetail = false;
      });
      _startAutoScroll(allImgs2.length);
    } catch(_) { if(mounted) setState(()=>_loadingDetail=false); }
  }

  Future<void> _map() async {
    final lat=_store["latitude"]?.toString()??""; final lng=_store["longitude"]?.toString()??"";
    final addr=_store["address"]??"";
    final url=(lat.isNotEmpty&&lng.isNotEmpty)
        ? "https://www.google.com/maps/dir/?api=1&destination=$lat,$lng"
        : "https://www.google.com/maps/search/?api=1&query=${Uri.encodeComponent(addr)}";
    await launchUrl(Uri.parse(url),mode:LaunchMode.externalApplication);
  }

  Future<void> _toggleFav() async {
    final id = _store["_id"]?.toString() ?? "";
    if(id.isEmpty) return;
    setState(()=>_isFav=!_isFav);
    await Api.toggleFavorite(widget.token, id);
  }

  Future<void> _submitRating(double r) async {
    if(_myRating!=null) return;
    final id=_store["_id"]?.toString()??"";
    if(id.isEmpty) return;
    setState(()=>_ratingSubmitting=true);
    try{
      final res = await Api.rateStore(widget.token,id,r);
      // Update local store rating with new average returned from server
      final newAvg = (res["avg_rating"] ?? res["rating"] ?? r) as num;
      setState((){
        _myRating=r;
        _store = Map<String,dynamic>.from(_store)
          ..["rating"] = newAvg.toDouble();
      });
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content:Text("⭐ Rated ${r.toStringAsFixed(1)} stars!"),
        backgroundColor:kPrimary));
    }catch(e){
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content:Text(e.toString().replaceAll("Exception: ","")),backgroundColor:Colors.red));
    }
    if(mounted)setState(()=>_ratingSubmitting=false);
  }

  Future<void> _share() async {
    final name = _store["store_name"]??"";
    final area = _store["area"]??"";
    final city = _store["city"]??"";
    final id   = _store["_id"]?.toString()??"";
    final shareText = "🏪 $name - $area, $city\nDiscover deals & earn loyalty points on OffrO!\nhttps://offro.app/store/$id";
    await showModalBottomSheet(
      context: context,
      backgroundColor: Colors.white,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(20))),
      builder: (_) => Padding(
        padding: const EdgeInsets.fromLTRB(20,16,20,32),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text("Share Store", style: TextStyle(fontWeight: FontWeight.w800, fontSize: 16, color: kText)),
          const SizedBox(height: 4),
          Text(shareText, style: const TextStyle(color: kMuted, fontSize: 12), maxLines: 3),
          const SizedBox(height: 16),
          Row(children: [
            _shareBtn(Icons.copy, "Copy Link", const Color(0xFF555555), () async {
              await Clipboard.setData(ClipboardData(text: shareText));
              if (context.mounted) {
                Navigator.pop(context);
                ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(content: Text("Copied to clipboard!"), backgroundColor: kPrimary, duration: Duration(seconds: 2)));
              }
            }),
            const SizedBox(width: 12),
            _shareBtn(Icons.messenger_outline_rounded, "WhatsApp", const Color(0xFF25D366), () async {
              final enc = Uri.encodeComponent(shareText);
              final wa = Uri.parse("whatsapp://send?text=$enc");
              if (await canLaunchUrl(wa)) await launchUrl(wa, mode: LaunchMode.externalApplication);
              if (context.mounted) Navigator.pop(context);
            }),
            const SizedBox(width: 12),
            _shareBtn(Icons.sms_outlined, "SMS", const Color(0xFF0066CC), () async {
              final enc = Uri.encodeComponent(shareText);
              final sms = Uri.parse("sms:?body=$enc");
              if (await canLaunchUrl(sms)) await launchUrl(sms, mode: LaunchMode.externalApplication);
              if (context.mounted) Navigator.pop(context);
            }),
          ]),
        ]),
      ),
    );
  }

  Widget _shareBtn(IconData icon, String label, Color color, VoidCallback onTap) =>
    GestureDetector(onTap: onTap, child: Column(mainAxisSize: MainAxisSize.min, children: [
      Container(width: 52, height: 52,
        decoration: BoxDecoration(color: color.withOpacity(0.12), borderRadius: BorderRadius.circular(14), border: Border.all(color: color.withOpacity(0.25))),
        child: Icon(icon, color: color, size: 24)),
      const SizedBox(height: 6),
      Text(label, style: TextStyle(color: color, fontSize: 11, fontWeight: FontWeight.w600)),
    ]));

  @override Widget build(BuildContext context){
    final s = _store;
    final deals = _loadingDetail ? <dynamic>[] : ((s["deals"] as List?) ?? <dynamic>[]);
    final city     = s['city']?.toString() ?? '';
    final area     = s['area']?.toString() ?? '';
    final address  = s['address']?.toString() ?? '';
    final phone    = s['phone']?.toString() ?? '';
    final desc     = (s['about']?.toString().isNotEmpty==true ? s['about']?.toString() : s['description']?.toString()) ?? '';
    final visitPts = (s['visit_points'] as num?)?.toInt() ?? 0;
    final rating   = (s['rating'] as num?)?.toDouble() ?? 0.0;
    final openTime = s['open_time']?.toString() ?? '';
    final closeTime= s['close_time']?.toString() ?? '';
    final costForTwo=s['cost_for_two']?.toString() ?? '';
    final dineIn   = s['dine_in'] == true;
    final category = s['category']?.toString() ?? '';
    final tags     = (s['tags'] as List?)?.map((t)=>t.toString()).toList() ?? [];
    final double? distKm = (s['distance_km'] as num?)?.toDouble();

    // Collect all images
    final imgs = (s["images"] as List?)?.map((x)=>x.toString()).toList() ?? [];
    final mainImg = s["image"]?.toString();
    final allImgs = [if(mainImg!=null&&mainImg.isNotEmpty) mainImg, ...imgs];

    final scrH = MediaQuery.of(context).size.height;

    return Scaffold(
      backgroundColor: kBg,
      body: Column(children:[
        // ── TOP IMAGE (50% screen) ──
        Container(
          height: scrH * 0.46,
          margin: const EdgeInsets.fromLTRB(12,12,12,0),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(22),
            border: Border.all(color:kBorder, width:1.2),
            boxShadow:[BoxShadow(color:Colors.black.withOpacity(.1),blurRadius:12,offset:const Offset(0,4))],
          ),
          clipBehavior: Clip.hardEdge,
          child: Stack(fit:StackFit.expand, children:[
            // Multi-image PageView
            allImgs.isEmpty
              ? Container(color:kPrimary,child:const Center(child:Icon(Icons.store,color:kLight,size:80)))
              : PageView.builder(
                  controller: _imgPc,
                  itemCount: allImgs.length,
                  onPageChanged:(i)=>setState(()=>_imgPage=i),
                  itemBuilder:(_,i){
                    final im = allImgs[i];
                    if(im.startsWith("data:image")){
                      try{ return Image.memory(base64Decode(im.split(",").last),fit:BoxFit.cover,gaplessPlayback:true); }catch(_){}
                    }
                    return Container(color:kPrimary,child:const Center(child:Icon(Icons.store,color:kLight,size:80)));
                  }),
            // gradient
            Positioned.fill(child:DecoratedBox(decoration:BoxDecoration(
              gradient:LinearGradient(begin:Alignment.topCenter,end:Alignment.bottomCenter,
                colors:[Colors.black.withOpacity(.35),Colors.transparent,Colors.transparent,Colors.black.withOpacity(.4)],
                stops:const[0,.2,.6,1]),
            ))),
            // back button
            Positioned(top:0,left:0,right:0,child:SafeArea(child:Padding(
              padding:const EdgeInsets.symmetric(horizontal:12,vertical:6),
              child:Row(children:[
                _imgBtn(Icons.arrow_back_ios_new_rounded,()=>Navigator.pop(context)),
                const Spacer(),
                _imgBtn(Icons.share_rounded,_share),
                const SizedBox(width:8),
                _imgBtn(_isFav?Icons.favorite_rounded:Icons.favorite_border_rounded,_toggleFav,
                  color:_isFav?Colors.red:Colors.white),
              ]),
            ))),
            // image dots
            if(allImgs.length>1) Positioned(bottom:14,left:0,right:0,child:Row(
              mainAxisAlignment:MainAxisAlignment.center,
              children:List.generate(allImgs.length,(i)=>AnimatedContainer(
                duration:const Duration(milliseconds:200),
                margin:const EdgeInsets.symmetric(horizontal:3),
                width:_imgPage==i?16:5,height:5,
                decoration:BoxDecoration(
                  color:_imgPage==i?Colors.white:Colors.white54,
                  borderRadius:BorderRadius.circular(3)),
              )),
            )),
            // distance badge
            if(distKm!=null) Positioned(bottom:14,right:14,child:Container(
              padding:const EdgeInsets.symmetric(horizontal:8,vertical:4),
              decoration:BoxDecoration(color:Colors.black54,borderRadius:BorderRadius.circular(10)),
              child:Row(mainAxisSize:MainAxisSize.min,children:[
                const Icon(Icons.near_me,color:Colors.white,size:11),const SizedBox(width:3),
                Text(distKm<1?"${(distKm*1000).round()}m away":"${distKm.toStringAsFixed(1)}km away",
                  style:const TextStyle(color:Colors.white,fontSize:10,fontWeight:FontWeight.w700)),
              ]),
            )),
          ]),
        ),

        // ── INFO + TABS ──
        Expanded(child:Container(
          margin: const EdgeInsets.fromLTRB(12,8,12,0),
          decoration: BoxDecoration(
            color: Colors.white,
            borderRadius: const BorderRadius.vertical(top:Radius.circular(20)),
            border: Border.all(color:kBorder, width:1),
          ),
          clipBehavior: Clip.hardEdge,
          child:Column(children:[
            // Store name + meta
            Padding(padding:const EdgeInsets.fromLTRB(16,12,16,0),child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
              Row(children:[
                Expanded(child:Text(s["store_name"]?.toString()??"",
                  style:const TextStyle(fontSize:20,fontWeight:FontWeight.w900,color:kText))),
                if(rating>0) Container(
                  padding:const EdgeInsets.symmetric(horizontal:8,vertical:4),
                  decoration:BoxDecoration(color:const Color(0xFFFFF3CC),borderRadius:BorderRadius.circular(10)),
                  child:Row(mainAxisSize:MainAxisSize.min,children:[
                    const Icon(Icons.star_rounded,color:Color(0xFFB8860B),size:13),const SizedBox(width:3),
                    Text(rating.toStringAsFixed(1),style:const TextStyle(color:Color(0xFFB8860B),fontWeight:FontWeight.w800,fontSize:12)),
                  ]),
                ),
              ]),
              const SizedBox(height:4),
              Row(children:[
                const Icon(Icons.location_on,color:kMuted,size:13),const SizedBox(width:3),
                Expanded(child:Text([area,city,address].where((x)=>x.isNotEmpty).join(", "),
                  style:const TextStyle(color:kMuted,fontSize:12),maxLines:1,overflow:TextOverflow.ellipsis)),
                if((s["category"]?.toString()??'').isNotEmpty)...[
                  const SizedBox(width:6),
                  Container(
                    padding:const EdgeInsets.symmetric(horizontal:7,vertical:2),
                    decoration:BoxDecoration(color:kLight,borderRadius:BorderRadius.circular(8)),
                    child:Text(s["category"].toString(),style:const TextStyle(color:kPrimary,fontSize:11,fontWeight:FontWeight.w700)),
                  ),
                ],
              ]),
              const SizedBox(height:8),
              // User rating widget
              _RatingWidget(
                currentRating: _myRating,
                submitting: _ratingSubmitting,
                onRate: _myRating==null ? _submitRating : null,
              ),
            ])),
            // Tabs
            Container(
              margin:const EdgeInsets.fromLTRB(16,8,16,0),
              decoration:BoxDecoration(color:const Color(0xFFEFF7F2),borderRadius:BorderRadius.circular(14)),
              child:TabBar(
                controller:_tabCtrl,
                indicator:BoxDecoration(color:kPrimary,borderRadius:BorderRadius.circular(12)),
                indicatorSize:TabBarIndicatorSize.tab,
                labelColor:Colors.white,
                unselectedLabelColor:kMuted,
                labelStyle:const TextStyle(fontWeight:FontWeight.w700,fontSize:11),
                padding:const EdgeInsets.all(3),
                tabs:const[
                  Tab(text:"Offers"),
                  Tab(text:"About"),
                  Tab(text:"Directions"),
                ],
              ),
            ),
            Expanded(child:TabBarView(controller:_tabCtrl,children:[
              // ── Offers tab ──
              _loadingDetail
                ? const Center(child:CircularProgressIndicator(color:kPrimary))
                : Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
                    // Scan & earn card — tappable, opens QR scanner
                    GestureDetector(
                      onTap:()=>Navigator.push(context,_route(QRPage(token:widget.token))),
                      child:Container(
                        margin:const EdgeInsets.fromLTRB(14,14,14,0),
                        padding:const EdgeInsets.all(14),
                        decoration:BoxDecoration(
                          gradient:const LinearGradient(colors:[Color(0xFF3E5F55),Color(0xFF5A8A7A)],begin:Alignment.centerLeft,end:Alignment.centerRight),
                          borderRadius:BorderRadius.circular(16),
                          boxShadow:[BoxShadow(color:const Color(0xFF3E5F55).withOpacity(.25),blurRadius:12,offset:const Offset(0,4))]),
                        child:Row(children:[
                          Container(width:52,height:52,decoration:BoxDecoration(color:Colors.white.withOpacity(.18),borderRadius:BorderRadius.circular(14)),
                            child:const Center(child:Icon(Icons.qr_code_scanner_rounded,color:Colors.white,size:28))),
                          const SizedBox(width:14),
                          Expanded(child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
                            Text(visitPts > 0 ? "Earn $visitPts pts on checkout" : "Earn loyalty points",
                              style:const TextStyle(color:Colors.white,fontSize:14,fontWeight:FontWeight.w800)),
                            const SizedBox(height:2),
                            const Text("Tap to scan store QR at checkout",
                              style:TextStyle(color:Colors.white70,fontSize:11)),
                          ])),
                          Container(padding:const EdgeInsets.symmetric(horizontal:10,vertical:6),
                            decoration:BoxDecoration(color:Colors.white.withOpacity(.22),borderRadius:BorderRadius.circular(10)),
                            child:const Text("Scan",style:TextStyle(color:Colors.white,fontWeight:FontWeight.w700,fontSize:12))),
                        ]),
                      ),
                    ),
                    if(deals.isEmpty)
                      const Expanded(child:Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
                        Icon(Icons.local_offer_outlined,color:kAccent,size:48),SizedBox(height:12),
                        Text("No active offers",style:TextStyle(color:kMuted))])))
                    else
                      Expanded(child:ListView(padding:const EdgeInsets.all(14),children:deals.map((d)=>Container(
                      margin:const EdgeInsets.only(bottom:10),
                      padding:const EdgeInsets.all(12),
                      decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(14),
                        border:Border.all(color:kBorder),
                        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.04),blurRadius:6,offset:const Offset(0,2))]),
                      child:Row(children:[
                        Container(width:48,height:48,decoration:BoxDecoration(color:kLight,borderRadius:BorderRadius.circular(12)),
                          child:Center(child:Text("${d['discount']??0}%",style:const TextStyle(color:kPrimary,fontWeight:FontWeight.w900,fontSize:13)))),
                        const SizedBox(width:10),
                        Expanded(child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
                          Text(d['title']??"",style:const TextStyle(color:kText,fontSize:13,fontWeight:FontWeight.bold)),
                          if((d['description']??"").toString().isNotEmpty)
                            Text(d['description'].toString(),style:const TextStyle(color:kMuted,fontSize:11),maxLines:2,overflow:TextOverflow.ellipsis),
                          Text("${d['start_date']??''} – ${d['end_date']??''}",style:const TextStyle(color:kMuted,fontSize:10)),
                        ])),
                      ]),
                    )).toList()))
                  ]),

              // ── About tab ──
              SingleChildScrollView(padding:const EdgeInsets.all(16),child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
                if(desc.isNotEmpty)...[const Text("About",style:TextStyle(fontWeight:FontWeight.w800,color:kText,fontSize:14)),const SizedBox(height:6),Text(desc,style:const TextStyle(color:kMuted,fontSize:13,height:1.6)),const SizedBox(height:14)],
                Wrap(spacing:8,runSpacing:6,children:[
                  if(openTime.isNotEmpty&&closeTime.isNotEmpty) _chip(Icons.access_time_rounded,"$openTime – $closeTime",const Color(0xFFE8F5E9),kPrimary),
                  if(dineIn) _chip(Icons.restaurant_rounded,"Dine-in",const Color(0xFFE3F2FD),Colors.blue),
                  if(costForTwo.isNotEmpty) _chip(Icons.currency_rupee_rounded,"₹$costForTwo for two",kBeige,kText),
                ]),
                if(tags.isNotEmpty)...[const SizedBox(height:12),Wrap(spacing:6,runSpacing:4,children:tags.map((t)=>Container(
                  padding:const EdgeInsets.symmetric(horizontal:8,vertical:3),
                  decoration:BoxDecoration(color:kBorder,borderRadius:BorderRadius.circular(8)),
                  child:Text("#$t",style:const TextStyle(color:kMuted,fontSize:11)),
                )).toList())],
              ])),

              // ── Directions tab ──
              Center(child:Padding(padding:const EdgeInsets.all(24),child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
                Container(padding:const EdgeInsets.all(20),decoration:BoxDecoration(color:kLight,borderRadius:BorderRadius.circular(20)),
                  child:const Icon(Icons.map_rounded,color:kPrimary,size:64)),
                const SizedBox(height:20),
                if(area.isNotEmpty||city.isNotEmpty) Text([area,city].where((x)=>x.isNotEmpty).join(", "),
                  style:const TextStyle(color:kText,fontSize:15,fontWeight:FontWeight.w700),textAlign:TextAlign.center),
                if(address.isNotEmpty)...[const SizedBox(height:4),Text(address,style:const TextStyle(color:kMuted,fontSize:13),textAlign:TextAlign.center)],
                const SizedBox(height:24),
                SizedBox(width:double.infinity,child:ElevatedButton.icon(
                  icon:const Icon(Icons.directions_rounded,color:Colors.white,size:18),
                  label:const Text("Open in Maps",style:TextStyle(color:Colors.white,fontWeight:FontWeight.w700)),
                  onPressed:_map,
                  style:ElevatedButton.styleFrom(backgroundColor:kPrimary,padding:const EdgeInsets.symmetric(vertical:14),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(14))),
                )),
              ]))),
            ])),
          ]),
        )),
      ]),
    );
  }

  Widget _imgBtn(IconData icon, VoidCallback onTap, {Color color=Colors.white})=>GestureDetector(
    onTap:onTap,
    child:Container(
      padding:const EdgeInsets.all(8),
      decoration:BoxDecoration(color:Colors.black38,borderRadius:BorderRadius.circular(12)),
      child:Icon(icon,color:color,size:20)));

  Widget _chip(IconData icon,String label,Color bg,Color fg)=>Container(
    padding:const EdgeInsets.symmetric(horizontal:10,vertical:5),
    decoration:BoxDecoration(color:bg,borderRadius:BorderRadius.circular(20)),
    child:Row(mainAxisSize:MainAxisSize.min,children:[
      Icon(icon,color:fg,size:13),const SizedBox(width:4),
      Text(label,style:TextStyle(color:fg,fontSize:11,fontWeight:FontWeight.w600)),
    ]));
}

// ─────────────────────── RATING WIDGET ───────────────────────
class _RatingWidget extends StatefulWidget {
  final double? currentRating;
  final bool submitting;
  final void Function(double)? onRate;
  const _RatingWidget({this.currentRating,required this.submitting,this.onRate});
  @override State<_RatingWidget> createState()=>_RatingWidgetState();
}
class _RatingWidgetState extends State<_RatingWidget>{
  double _hover=0;
  @override Widget build(BuildContext ctx){
    final rated = widget.currentRating!=null;
    return Row(children:[
      ...List.generate(5,(i){
        final filled = rated ? (i<(widget.currentRating!.round())) : (i<_hover);
        return GestureDetector(
          onTap: rated||widget.onRate==null ? null : (){widget.onRate!(i+1.0);},
          child:Padding(
            padding:const EdgeInsets.only(right:3),
            child:Icon(filled?Icons.star_rounded:Icons.star_outline_rounded,
              color:filled?const Color(0xFFFFD700):kMuted,size:22)));
      }),
      const SizedBox(width:8),
      if(widget.submitting) const SizedBox(width:16,height:16,child:CircularProgressIndicator(strokeWidth:2,color:kPrimary))
      else Text(
        rated?"Your rating: ${widget.currentRating!.toStringAsFixed(1)} ⭐":"Tap to rate",
        style:TextStyle(color:rated?kPrimary:kMuted,fontSize:12,fontWeight:rated?FontWeight.w700:FontWeight.w400)),
    ]);
  }
}


// ─────────────────────── QR SCANNER ───────────────────────
class QRPage extends StatefulWidget {
  final String token; final VoidCallback? onDone;
  const QRPage({super.key,required this.token,this.onDone});
  @override State<QRPage> createState()=>_QRState();
}
class _QRState extends State<QRPage>{
  bool _scanned=false;
  @override Widget build(BuildContext ctx)=>Scaffold(
    appBar:AppBar(title:const Text("Scan Store QR"),backgroundColor:kPrimary,foregroundColor:Colors.white),
    body:Stack(children:[
      MobileScanner(onDetect:(cap) async {
        if(_scanned)return; final raw = cap.barcodes.isNotEmpty ? cap.barcodes.first.rawValue ?? "" : ""; if(raw.isEmpty)return;
        setState(()=>_scanned=true);
        String? sid=raw.contains("store_id=")?raw.split("store_id=").last.split("&").first.trim():null;
        if(sid==null||sid.isEmpty){if(!mounted)return;Navigator.pop(ctx);ScaffoldMessenger.of(ctx).showSnackBar(const SnackBar(content:Text("❌ Invalid QR")));return;}
        try{
          final res=await Api.redeemQR(sid,widget.token); widget.onDone?.call();
          if(!mounted)return; Navigator.pop(ctx);
          showDialog(context:ctx,builder:(_)=>AlertDialog(shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(16)),
            title:Row(children:[const Icon(Icons.check_circle,color:kPrimary),const SizedBox(width:8),const Text("Points Added!")]),
            content:Text("${res["message"]??"Done!"}\n\n🔐 Store QR has been refreshed for security."),
            actions:[TextButton(onPressed:()=>Navigator.pop(ctx),child:const Text("Great!",style:TextStyle(color:kPrimary)))]));
        }catch(e){if(!mounted)return;Navigator.pop(ctx);ScaffoldMessenger.of(ctx).showSnackBar(SnackBar(content:Text(e.toString().replaceAll("Exception: ",""))));}
      }),
      Center(child:Container(width:220,height:220,decoration:BoxDecoration(border:Border.all(color:kPrimary,width:3),borderRadius:BorderRadius.circular(16)))),
      const Positioned(bottom:60,left:0,right:0,child:Text("Point at store QR code",textAlign:TextAlign.center,style:TextStyle(color:Colors.white,fontSize:14))),
    ]),
  );
}

// ─────────────────────── WALLET PAGE ───────────────────────
class WalletPage extends StatefulWidget {
  final String token; const WalletPage({super.key,required this.token});
  @override State<WalletPage> createState()=>_WalletState();
}
class _WalletState extends State<WalletPage>{
  int vp=0,pp=0; bool loading=true;
  @override void initState(){super.initState();_load();}
  Future<void> _load() async { try{final d=await Api.getWallet(widget.token);if(mounted)setState((){vp=d["visit_points"]??0;pp=d["pool_points"]??0;loading=false;});}catch(_){if(mounted)setState(()=>loading=false);} }
  Future<void> _withdraw() async {
    try{final r=await Api.withdraw(widget.token,200);if(!mounted)return;ScaffoldMessenger.of(context).showSnackBar(SnackBar(content:Text(r["message"]??"Done")));_load();}
    catch(e){if(!mounted)return;ScaffoldMessenger.of(context).showSnackBar(SnackBar(content:Text(e.toString().replaceAll("Exception: ",""))));}
  }
  @override Widget build(BuildContext ctx){
    int total=vp;
    return Scaffold(appBar:AppBar(title:const Text("My Wallet"),backgroundColor:kPrimary,foregroundColor:Colors.white),backgroundColor:kBg,
      body:loading?const Center(child:CircularProgressIndicator(color:kPrimary)):SingleChildScrollView(padding:const EdgeInsets.all(20),child:Column(children:[
        const SizedBox(height:10),
        Container(width:double.infinity,padding:const EdgeInsets.all(24),decoration:BoxDecoration(color:kPrimary,borderRadius:BorderRadius.circular(18)),
          child:Column(children:[
            const Text("Total Points",style:TextStyle(color:kLight,fontSize:14)),
            const SizedBox(height:8),
            Text("$total",style:const TextStyle(color:Colors.white,fontSize:46,fontWeight:FontWeight.bold)),
            const Text("points",style:TextStyle(color:kAccent)),
          ])),
        const SizedBox(height:20),
        Container(padding:const EdgeInsets.all(16),decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(12),border:Border.all(color:kBorder)),
          child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
            const Text("💡 Points Info",style:TextStyle(fontWeight:FontWeight.bold,color:kPrimary,fontSize:14)),const SizedBox(height:8),
            _info("💰 Conversion","10 Points = ₹1 of Gift Voucher"),_info("📤 Min Withdrawal","200 Points = ₹20 Gift Voucher"),
            _info("🎁 Reward","Amazon or Flipkart Gift Vouchers"),_info("⏱️ Validity","Points never expire"),
            _info("📋 Processing","Delivery of Gift Voucher: 3-5 Business days"),
          ])),
        const SizedBox(height:20),
        SizedBox(width:double.infinity,child:ElevatedButton(
          onPressed:total>=200?_withdraw:null,
          style:ElevatedButton.styleFrom(backgroundColor:kPrimary,padding:const EdgeInsets.symmetric(vertical:14),shape:RoundedRectangleBorder(borderRadius:BorderRadius.circular(12)),disabledBackgroundColor:kAccent),
          child:Text(total>=200?"Withdraw (200 pts = ₹20 Gift Voucher)":"Need ${200-total} more pts to redeem Gift Voucher",style:const TextStyle(color:Colors.white,fontSize:14)))),
        const SizedBox(height:8),
        const Text("10 pts = ₹1 Gift Voucher  •  Min 200 pts  •  3-5 business days delivery",textAlign:TextAlign.center,style:TextStyle(color:kMuted,fontSize:11)),
      ])));
  }
  Widget _box(String t,int v,Color bg,IconData ico)=>Container(padding:const EdgeInsets.all(16),decoration:BoxDecoration(color:bg,borderRadius:BorderRadius.circular(14)),
    child:Column(children:[Row(mainAxisAlignment:MainAxisAlignment.center,children:[Icon(ico,color:kPrimary,size:14),const SizedBox(width:4),Text(t,style:const TextStyle(color:kPrimary,fontSize:12,fontWeight:FontWeight.w600))]),const SizedBox(height:8),Text("$v",style:const TextStyle(fontSize:28,fontWeight:FontWeight.bold,color:kText))]));
  Widget _info(String k,String v)=>Padding(
    padding:const EdgeInsets.only(bottom:5),
    child:Row(crossAxisAlignment:CrossAxisAlignment.start,children:[
      Text("$k: ",style:const TextStyle(fontWeight:FontWeight.w600,fontSize:12,color:kPrimary)),
      Expanded(child:Text(v,style:const TextStyle(fontSize:12,color:kText))),
    ]));
}

// ─────────────────────── HISTORY PAGE ───────────────────────
class HistoryPage extends StatefulWidget {
  final String token; const HistoryPage({super.key,required this.token});
  @override State<HistoryPage> createState()=>_HistoryState();
}
class _HistoryState extends State<HistoryPage>{
  List _h=[]; bool _l=true;
  @override void initState(){super.initState();_load();}
  Future<void> _load() async { _h=await Api.getRedemptions(widget.token); if(mounted)setState(()=>_l=false); }
  @override Widget build(BuildContext ctx)=>Scaffold(
    appBar:AppBar(title:const Text("Scan History"),backgroundColor:kPrimary,foregroundColor:Colors.white),backgroundColor:kBg,
    body:_l?const Center(child:CircularProgressIndicator(color:kPrimary)):
    _h.isEmpty?Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[buildLogo(44,kAccent),const SizedBox(height:12),const Text("No scans yet",style:TextStyle(color:kMuted,fontSize:16))])):
    ListView.separated(padding:const EdgeInsets.all(16),itemCount:_h.length,separatorBuilder:(_,__)=>const SizedBox(height:8),
      itemBuilder:(_,i){
        final h=_h[i] as Map;
        final String? storeImg = h["store_image"]?.toString();
        final String dateRaw = h["date"]?.toString() ?? "";
        // Try to format date nicely
        String dateLabel = dateRaw;
        try {
          final dt = DateTime.parse(dateRaw);
          final months=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
          dateLabel = "${dt.day} ${months[dt.month-1]} ${dt.year}  ${dt.hour.toString().padLeft(2,'0')}:${dt.minute.toString().padLeft(2,'0')}";
        } catch(_) {}
        return Container(padding:const EdgeInsets.all(12),decoration:BoxDecoration(color:Colors.white,borderRadius:BorderRadius.circular(14),border:Border.all(color:kBorder)),
          child:Row(children:[
            // store image or fallback icon
            ClipRRect(borderRadius:BorderRadius.circular(10),
              child: SizedBox(width:48,height:48,
                child: storeImg!=null && storeImg.startsWith("data:image")
                  ? (() { try { return Image.memory(base64Decode(storeImg.split(",").last),fit:BoxFit.cover,gaplessPlayback:true); } catch(_) { return const SizedBox(); } })()
                  : Container(color:kLight,child:const Icon(Icons.store_rounded,color:kPrimary,size:22)))),
            const SizedBox(width:12),
            Expanded(child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[
              Text(h["store_name"]??"",style:const TextStyle(fontWeight:FontWeight.bold,color:kText,fontSize:13)),
              const SizedBox(height:3),
              Row(children:[
                const Icon(Icons.calendar_today_rounded,color:kMuted,size:11),const SizedBox(width:4),
                Text(dateLabel,style:const TextStyle(color:kMuted,fontSize:11)),
              ]),
            ])),
            Container(
              padding:const EdgeInsets.symmetric(horizontal:10,vertical:5),
              decoration:BoxDecoration(color:kLight,borderRadius:BorderRadius.circular(12)),
              child:Text("+${h['points']} pts",style:const TextStyle(color:kPrimary,fontWeight:FontWeight.bold,fontSize:13)),
            )]));
      },));
}

// ─────────────────────── RAZORPAY WEBVIEW ───────────────────────



// ─────────────────────── GRID STORE CARD (2-per-row, image overlay) ───────────────────────
class _GridStoreCard extends StatefulWidget {
  final Map store;
  const _GridStoreCard({super.key, required this.store});
  @override State<_GridStoreCard> createState() => _GridStoreCardState();
}
class _GridStoreCardState extends State<_GridStoreCard> {
  int _imgIdx = 0;
  late final PageController _pc;

  @override void initState() { super.initState(); _pc = PageController(); }
  @override void dispose() { _pc.dispose(); super.dispose(); }

  Widget _imgAt(String img, String name) {
    if (img.startsWith("data:image")) {
      try { return Image.memory(base64Decode(img.split(",").last),fit:BoxFit.cover,width:double.infinity,height:double.infinity,gaplessPlayback:true); }
      catch(_) {}
    }
    if (img.startsWith("http")) {
      return CachedNetworkImage(imageUrl:img, fit:BoxFit.cover, width:double.infinity, height:double.infinity,
        placeholder:(_,__)=>_fallback(name),
        errorWidget:(_,__,___)=>_fallback(name));
    }
    return _fallback(name);
  }

  @override
  Widget build(BuildContext context) {
    final Map store = widget.store;
    final imgs   = (store["images"] as List?)?.map((x)=>x.toString()).toList() ?? [];
    final img    = store["image"]?.toString();
    final img2   = store["image2"]?.toString();
    final allImgs = [if(img!=null&&img.isNotEmpty) img, if(img2!=null&&img2.isNotEmpty) img2, ...imgs];
    final String name     = store["store_name"]?.toString() ?? "";
    final String category = store["category"]?.toString() ?? "";
    final String area     = store["area"]?.toString() ?? "";
    final int visitPts    = ((store["visit_points"] as num?)?.toInt() ?? 0);
    final String offerStr = (store["offer"] ?? "") as String;
    final int dealCount   = ((store["deal_count"] ?? 0) as num).toInt();
    final bool hasDeal    = offerStr.isNotEmpty && dealCount > 0;
    final double rating   = ((store["rating"] as num?)?.toDouble() ?? 0.0);
    final double? distKm  = (store["distance_km"] as num?)?.toDouble();
    final pm              = hasDeal ? RegExp(r'([\d.]+)%').firstMatch(offerStr) : null;
    final String dealLbl  = pm != null ? "${pm.group(1)}% OFF" : (hasDeal?"Deal":"");

    // Image layer: swipeable PageView if multiple images, else single
    Widget imgLayer = allImgs.isEmpty
        ? _fallback(name)
        : (allImgs.length == 1
            ? _imgAt(allImgs.first, name)
            : PageView.builder(
                controller: _pc,
                physics: const BouncingScrollPhysics(),
                itemCount: allImgs.length,
                onPageChanged: (i) => setState(() => _imgIdx = i),
                itemBuilder: (_, i) => _imgAt(allImgs[i], name),
              ));

    return ClipRRect(
      borderRadius: BorderRadius.circular(16),
      child: Stack(fit:StackFit.expand, children:[
        imgLayer,
        // tiny dot indicator at top-right when multiple images
        if(allImgs.length > 1)
          Positioned(top:6,right:6,child:Container(
            padding:const EdgeInsets.symmetric(horizontal:5,vertical:2),
            decoration:BoxDecoration(color:Colors.black45,borderRadius:BorderRadius.circular(8)),
            child:Row(mainAxisSize:MainAxisSize.min, children:
              List.generate(allImgs.length,(i)=>Container(
                width: i==_imgIdx?10:5, height:4, margin:const EdgeInsets.symmetric(horizontal:1.5),
                decoration:BoxDecoration(
                  color: i==_imgIdx?Colors.white:Colors.white54,
                  borderRadius:BorderRadius.circular(2)),
              )),
            ),
          )),
        // gradient bottom
        Positioned.fill(child:DecoratedBox(decoration:BoxDecoration(
          gradient:LinearGradient(
            begin:Alignment.topCenter, end:Alignment.bottomCenter,
            colors:[Colors.transparent,Colors.transparent,Colors.black.withOpacity(.55),Colors.black.withOpacity(.88)],
            stops:const[0,.4,.7,1]),
        ))),
        // deal badge
        if(hasDeal) Positioned(top:8,left:8,child:Container(
          padding:const EdgeInsets.symmetric(horizontal:6,vertical:3),
          decoration:BoxDecoration(color:Colors.deepOrange.withOpacity(.9),borderRadius:BorderRadius.circular(8)),
          child:Text("🔥 $dealLbl",style:const TextStyle(color:Colors.white,fontSize:9,fontWeight:FontWeight.w800)),
        )),
        // bottom info
        Positioned(bottom:0,left:0,right:0,child:Padding(
          padding:const EdgeInsets.fromLTRB(8,0,8,8),
          child:Column(crossAxisAlignment:CrossAxisAlignment.start,mainAxisSize:MainAxisSize.min,children:[
            Text(name,style:const TextStyle(color:Colors.white,fontSize:12,fontWeight:FontWeight.w800,
              shadows:[Shadow(blurRadius:4,color:Colors.black87)]),maxLines:1,overflow:TextOverflow.ellipsis),
            const SizedBox(height:2),
            Row(children:[
              if(rating>0)...[
                const Icon(Icons.star_rounded,color:Color(0xFFFFD700),size:10),
                const SizedBox(width:2),
                Text(rating.toStringAsFixed(1),style:const TextStyle(color:Colors.white,fontSize:9,fontWeight:FontWeight.w700)),
                const SizedBox(width:4),
              ],
              if(category.isNotEmpty) Container(
                margin:const EdgeInsets.only(right:4),
                padding:const EdgeInsets.symmetric(horizontal:5,vertical:2),
                decoration:BoxDecoration(color:kPrimary.withOpacity(.80),borderRadius:BorderRadius.circular(6)),
                child:Text(category,style:const TextStyle(color:Colors.white,fontSize:8,fontWeight:FontWeight.w700)),
              ),
              if(distKm!=null) Container(
                margin:const EdgeInsets.only(right:4),
                padding:const EdgeInsets.symmetric(horizontal:5,vertical:2),
                decoration:BoxDecoration(color:Colors.black45,borderRadius:BorderRadius.circular(6)),
                child:Row(mainAxisSize:MainAxisSize.min,children:[
                  const Icon(Icons.near_me_rounded,color:Colors.white70,size:8),const SizedBox(width:2),
                  Text(distKm<1?"${(distKm*1000).round()}m away":"${distKm.toStringAsFixed(1)}km away",
                    style:const TextStyle(color:Colors.white,fontSize:8,fontWeight:FontWeight.w700)),
                ]),
              ),
              const Icon(Icons.location_on,color:Colors.white60,size:10),
              Expanded(child:Text(area,style:const TextStyle(color:Colors.white70,fontSize:9),overflow:TextOverflow.ellipsis)),
            ]),
          ]),
        )),
      ]),
    );
  }

  Widget _fallback(String name)=>Container(
    color:const Color(0xFF2a4a40),
    child:Center(child:Icon(Icons.store,color:kLight,size:36)),
  );
}

// ─────────────────────── BIG CAROUSEL CARD (New In Town) ───────────────────────
// StatefulWidget so we decode base64 ONCE in initState — never re-decodes during drag
class _BigCarouselCard extends StatefulWidget {
  final Map store;
  const _BigCarouselCard({super.key, required this.store});
  @override State<_BigCarouselCard> createState() => _BigCarouselCardState();
}
class _BigCarouselCardState extends State<_BigCarouselCard> {
  Uint8List? _imgBytes;

  @override void initState() {
    super.initState();
    _decodeImage();
  }

  @override void didUpdateWidget(_BigCarouselCard old) {
    super.didUpdateWidget(old);
    if (old.store["image"] != widget.store["image"]) _decodeImage();
  }

  void _decodeImage() {
    final img = widget.store["image"]?.toString() ?? "";
    if (img.startsWith("data:image")) {
      try { _imgBytes = base64Decode(img.split(",").last); } catch(_) { _imgBytes = null; }
    } else {
      _imgBytes = null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final img = widget.store["image"]?.toString() ?? "";
    final Map store = widget.store;
    final String name     = store["store_name"]?.toString() ?? "";
    final String category = store["category"]?.toString() ?? "";
    final String area     = store["area"]?.toString() ?? "";
    final String city     = store["city"]?.toString() ?? "";
    final int visitPts    = ((store["visit_points"] as num?)?.toInt() ?? 0);
    final String offerStr = (store["offer"] ?? "") as String;
    final int dealCount   = ((store["deal_count"] ?? 0) as num).toInt();
    final bool hasDeal    = offerStr.isNotEmpty && dealCount > 0;
    final pm              = hasDeal ? RegExp(r'([\d.]+)%').firstMatch(offerStr) : null;
    final String dealLbl  = pm != null ? "🔥 ${pm.group(1)}% OFF" : (hasDeal?"🔥 Deal":"");
    final double? distKm  = (store["distance_km"] as num?)?.toDouble();
    final double rating    = ((store["rating"] as num?)?.toDouble() ?? 0.0);

    Widget imgW;
    if (_imgBytes != null) {
      // Pre-decoded bytes — Image.memory just renders, zero decode work each frame
      imgW = Image.memory(_imgBytes!, fit:BoxFit.cover,
        width:double.infinity, height:double.infinity, gaplessPlayback:true);
    } else if (img.startsWith("http")) {
      imgW = CachedNetworkImage(imageUrl:img, fit:BoxFit.cover,
        width:double.infinity, height:double.infinity,
        placeholder:(_,__)=>_fallback(name),
        errorWidget:(_,__,___)=>_fallback(name));
    } else {
      imgW = _fallback(name);
    }

    return Container(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(24),
        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.2),blurRadius:18,offset:const Offset(0,6))],
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(24),
        child: Stack(fit:StackFit.expand, children:[
          Positioned.fill(child: imgW),
          // gradient
          Positioned.fill(child:DecoratedBox(decoration:BoxDecoration(
            gradient:LinearGradient(
              begin:Alignment.topCenter, end:Alignment.bottomCenter,
              colors:[Colors.transparent,Colors.transparent,Colors.black.withOpacity(.7),Colors.black.withOpacity(.92)],
              stops:const[0,.45,.75,1]),
          ))),
          // NEW IN TOWN badge removed (NIT fix 3)
          // duplicate distance badge removed (NIT fix 4)
          // deal badge moved to top-LEFT (NIT fix 5)
          if(hasDeal) Positioned(top:14,left:14,child:Container(
            padding:const EdgeInsets.symmetric(horizontal:10,vertical:5),
            decoration:BoxDecoration(color:Colors.deepOrange.withOpacity(.93),borderRadius:BorderRadius.circular(12)),
            child:Text(dealLbl,style:const TextStyle(color:Colors.white,fontWeight:FontWeight.w900,fontSize:12)),
          )),
          // bottom info
          Positioned(bottom:0,left:0,right:0,child:Padding(
            padding:const EdgeInsets.fromLTRB(16,0,16,16),
            child:Column(crossAxisAlignment:CrossAxisAlignment.start,mainAxisSize:MainAxisSize.min,children:[
              Text(name,style:const TextStyle(color:Colors.white,fontSize:22,fontWeight:FontWeight.w900,
                shadows:[Shadow(blurRadius:6,color:Colors.black87)]),maxLines:1,overflow:TextOverflow.ellipsis),
              const SizedBox(height:4),
              Row(children:[
                const Icon(Icons.location_on,color:kLight,size:12),const SizedBox(width:3),
                Expanded(child:Text([area,city].where((x)=>x.isNotEmpty).join(", "),
                  style:const TextStyle(color:Colors.white70,fontSize:12),overflow:TextOverflow.ellipsis)),
              ]),
              // Rating + category + distance — all in one row
              const SizedBox(height:6),
              Row(children:[
                if(rating>0) Container(
                  margin:const EdgeInsets.only(right:6),
                  padding:const EdgeInsets.symmetric(horizontal:8,vertical:3),
                  decoration:BoxDecoration(color:const Color(0xFFB8860B).withOpacity(.9),borderRadius:BorderRadius.circular(10)),
                  child:Row(mainAxisSize:MainAxisSize.min,children:[
                    const Icon(Icons.star_rounded,color:Colors.white,size:12),const SizedBox(width:3),
                    Text(rating.toStringAsFixed(1),style:const TextStyle(color:Colors.white,fontSize:11,fontWeight:FontWeight.w800)),
                  ])),
                if(category.isNotEmpty) Container(
                  margin:const EdgeInsets.only(right:6),
                  padding:const EdgeInsets.symmetric(horizontal:8,vertical:3),
                  decoration:BoxDecoration(color:kPrimary.withOpacity(.85),borderRadius:BorderRadius.circular(10)),
                  child:Text(category,style:const TextStyle(color:Colors.white,fontSize:10,fontWeight:FontWeight.w700)),
                ),
                if(distKm!=null) Container(
                  margin:const EdgeInsets.only(right:6),
                  padding:const EdgeInsets.symmetric(horizontal:8,vertical:3),
                  decoration:BoxDecoration(color:Colors.black54,borderRadius:BorderRadius.circular(10)),
                  child:Row(mainAxisSize:MainAxisSize.min,children:[
                    const Icon(Icons.near_me_rounded,color:Colors.white,size:11),const SizedBox(width:3),
                    Text(distKm!<1?"${(distKm*1000).round()}m away":"${distKm.toStringAsFixed(1)}km away",style:const TextStyle(color:Colors.white,fontSize:10,fontWeight:FontWeight.w700)),
                  ])),
              ]),

            ]),
          )),
        ]),
      ),
    );
  }

  Widget _fallback(String name)=>Container(
    decoration:const BoxDecoration(gradient:LinearGradient(colors:[Color(0xFF2a4a40),Color(0xFF3E5F55)],begin:Alignment.topLeft,end:Alignment.bottomRight)),
    child:Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
      const Icon(Icons.store_mall_directory_outlined,size:64,color:kLight),
      const SizedBox(height:10),
      Text(name,style:const TextStyle(color:Colors.white,fontSize:16,fontWeight:FontWeight.bold),textAlign:TextAlign.center),
    ])),
  );
} // end _BigCarouselCardState

// ─────────────────────── TOP STORE CARD (Horizontal list card) ───────────────────────
class _TopStoreCard extends StatelessWidget {
  final Map store;
  const _TopStoreCard({required this.store});

  @override
  Widget build(BuildContext context) {
    final img = store["image"];
    final String name     = store["store_name"]?.toString() ?? "";
    final String category = store["category"]?.toString() ?? "";
    final String area     = store["area"]?.toString() ?? "";
    final String city     = store["city"]?.toString() ?? "";
    final int visitPts    = ((store["visit_points"] as num?)?.toInt() ?? 0);
    final String offerStr = (store["offer"] ?? "") as String;
    final int dealCount   = ((store["deal_count"] ?? 0) as num).toInt();
    final bool hasDeal    = offerStr.isNotEmpty && dealCount > 0;
    final pm              = hasDeal ? RegExp(r'([\d.]+)%').firstMatch(offerStr) : null;
    final String dealLbl  = pm != null ? "${pm.group(1)}% OFF" : (hasDeal?"Deal":"");

    final double? distKm  = (store["distance_km"] as num?)?.toDouble();
    final img2   = store["image2"]?.toString();
    final imgs2 = (store["images"] as List?)?.map((x)=>x.toString()).toList() ?? [];
    final allImgsT = [if(img!=null&&img.toString().isNotEmpty) img.toString(), if(img2!=null&&img2.isNotEmpty) img2, ...imgs2];

    Widget buildImgItem(String im) {
      if (im.startsWith("data:image")) {
        try { return Image.memory(base64Decode(im.split(",").last),fit:BoxFit.cover,width:double.infinity,height:double.infinity,gaplessPlayback:true); }
        catch(_) {}
      }
      if (im.startsWith("http")) {
        return CachedNetworkImage(imageUrl:im, fit:BoxFit.cover, width:double.infinity, height:double.infinity,
          placeholder:(_,__)=>_fallback(name),
          errorWidget:(_,__,___)=>_fallback(name));
      }
      return _fallback(name);
    }

    final Widget imgSection = allImgsT.isEmpty
      ? _fallback(name)
      : allImgsT.length == 1
        ? buildImgItem(allImgsT.first)
        : PageView.builder(
            physics: const BouncingScrollPhysics(),
            itemCount: allImgsT.length,
            itemBuilder: (_, i) => buildImgItem(allImgsT[i]),
          );

    return Container(
      decoration:BoxDecoration(
        color:Colors.white,
        borderRadius:BorderRadius.circular(16),
        boxShadow:[BoxShadow(color:Colors.black.withOpacity(.07),blurRadius:12,offset:const Offset(0,3))],
      ),
      child:Row(children:[
        // left image (swipe left/right for multiple)
        ClipRRect(
          borderRadius:const BorderRadius.horizontal(left:Radius.circular(16)),
          child:SizedBox(width:100,height:90,child:imgSection),
        ),
        // right info
        Expanded(child:Padding(
          padding:const EdgeInsets.symmetric(horizontal:12,vertical:10),
          child:Column(crossAxisAlignment:CrossAxisAlignment.start,mainAxisAlignment:MainAxisAlignment.center,children:[
            if(category.isNotEmpty) Container(
              margin:const EdgeInsets.only(bottom:4),
              padding:const EdgeInsets.symmetric(horizontal:7,vertical:2),
              decoration:BoxDecoration(color:kLight,borderRadius:BorderRadius.circular(6)),
              child:Text(category,style:const TextStyle(color:kPrimary,fontSize:10,fontWeight:FontWeight.w700)),
            ),
            Text(name,style:const TextStyle(color:kText,fontSize:14,fontWeight:FontWeight.w800),maxLines:1,overflow:TextOverflow.ellipsis),
            const SizedBox(height:3),
            Row(children:[
              const Icon(Icons.location_on,color:kMuted,size:11),const SizedBox(width:2),
              Expanded(child:Text([area,city].where((x)=>x.isNotEmpty).join(", "),style:const TextStyle(color:kMuted,fontSize:11),overflow:TextOverflow.ellipsis)),
              if(distKm!=null) Container(
                margin:const EdgeInsets.only(left:4),
                padding:const EdgeInsets.symmetric(horizontal:5,vertical:2),
                decoration:BoxDecoration(color:kLight,borderRadius:BorderRadius.circular(6)),
                child:Row(mainAxisSize:MainAxisSize.min,children:[
                  const Icon(Icons.near_me_rounded,color:kPrimary,size:9),const SizedBox(width:2),
                  Text(distKm<1?"${(distKm*1000).round()}m away":"${distKm.toStringAsFixed(1)}km away",
                    style:const TextStyle(color:kPrimary,fontSize:9,fontWeight:FontWeight.w700)),
                ]),
              ),
            ]),
            if(visitPts>0||hasDeal) const SizedBox(height:5),
            Row(children:[
              if(visitPts>0) Container(
                padding:const EdgeInsets.symmetric(horizontal:7,vertical:3),
                decoration:BoxDecoration(color:const Color(0xFFFFF3CC),borderRadius:BorderRadius.circular(8)),
                child:Row(mainAxisSize:MainAxisSize.min,children:[
                  const Icon(Icons.star_rounded,color:Color(0xFFB8860B),size:11),const SizedBox(width:3),
                  Text("$visitPts pts",style:const TextStyle(color:Color(0xFFB8860B),fontSize:10,fontWeight:FontWeight.w700)),
                ]),
              ),
              if(visitPts>0&&hasDeal) const SizedBox(width:5),
              if(hasDeal) Container(
                padding:const EdgeInsets.symmetric(horizontal:7,vertical:3),
                decoration:BoxDecoration(color:Colors.deepOrange.withOpacity(.1),borderRadius:BorderRadius.circular(8)),
                child:Text("🔥 $dealLbl",style:const TextStyle(color:Colors.deepOrange,fontSize:10,fontWeight:FontWeight.w700)),
              ),
            ]),
          ]),
        )),
        // arrow
        Padding(padding:const EdgeInsets.only(right:12),child:const Icon(Icons.arrow_forward_ios_rounded,color:kBorder,size:14)),
      ]),
    );
  }

  Widget _fallback(String name)=>Container(
    color:kAccent,
    child:Center(child:Column(mainAxisAlignment:MainAxisAlignment.center,children:[
      const Icon(Icons.store,color:kPrimary,size:28),
    ])),
  );
}

// ─────────────────────── STACKED STORE CARDS ───────────────────────
class _StackedCards extends StatelessWidget {
  final List<Map<String,dynamic>> stores;
  final int page;
  final PageController pc;
  final ValueChanged<int> onPageChanged;
  final ValueChanged<Map> onTap;
  final VoidCallback onMore;
  final VoidCallback onCategory;

  const _StackedCards({
    required this.stores, required this.page, required this.pc,
    required this.onPageChanged, required this.onTap,
    required this.onMore, required this.onCategory,
  });

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.of(context).size;
    final cardH = size.height * 0.74;

    return Stack(
      children: [
        // ── Page view with peek ──
        PageView.builder(
          controller: PageController(viewportFraction:0.90),
          itemCount: stores.length,
          onPageChanged: onPageChanged,
          itemBuilder: (_, i) {
            final store = stores[i];
            return Padding(
              // slight padding keeps card in frame with small peek of next
              padding: const EdgeInsets.only(left:8, right:8, top:4, bottom:0),
              child: GestureDetector(
                onTap: () => onTap(store),
                child: _StoreCardItem(store: store, cardH: cardH),
              ),
            );
          },
        ),

        // ── Dot indicator ──
        Positioned(
          bottom: 100,
          left: 0, right: 0,
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: List.generate(stores.length > 8 ? 8 : stores.length, (i) {
              final active = i == (page % (stores.length > 8 ? 8 : stores.length));
              return AnimatedContainer(
                duration: const Duration(milliseconds: 250),
                margin: const EdgeInsets.symmetric(horizontal:3),
                width: active ? 20 : 6,
                height: 6,
                decoration: BoxDecoration(
                  color: active ? Colors.white : Colors.white38,
                  borderRadius: BorderRadius.circular(3),
                ));
            }),
          ),
        ),

        // ── Bottom action row ──
        Positioned(
          bottom: 24, left: 16, right: 16,
          child: Row(
            children: [
              Expanded(
                child: ElevatedButton.icon(
                  onPressed: onCategory,
                  icon: const Icon(Icons.grid_view_rounded, size: 16),
                  label: const Text("Category", style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.white,
                    foregroundColor: kPrimary,
                    padding: const EdgeInsets.symmetric(vertical:13),
                    elevation: 4,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(24)),
                  ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: ElevatedButton.icon(
                  onPressed: onMore,
                  icon: const Icon(Icons.person_outline_rounded, size: 16),
                  label: const Text("More", style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: kLight,
                    foregroundColor: kPrimary,
                    padding: const EdgeInsets.symmetric(vertical:13),
                    elevation: 4,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(24)),
                  ),
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

// ─────────────────────── SINGLE STORE CARD ITEM ───────────────────────
class _StoreCardItem extends StatefulWidget {
  final Map store;
  final double cardH;
  const _StoreCardItem({super.key, required this.store, required this.cardH});
  @override State<_StoreCardItem> createState() => _StoreCardItemState();
}
class _StoreCardItemState extends State<_StoreCardItem> {
  int _imgIdx = 0;
  late final PageController _pc;
  @override void initState() { super.initState(); _pc = PageController(); }
  @override void dispose() { _pc.dispose(); super.dispose(); }

  Widget _imgAt(String img, String name) {
    if (img.startsWith("data:image")) {
      try { return Image.memory(base64Decode(img.split(",").last),fit:BoxFit.cover,gaplessPlayback:true,width:double.infinity,height:double.infinity); }
      catch(_) {}
    }
    if (img.startsWith("http")) {
      return CachedNetworkImage(imageUrl:img, fit:BoxFit.cover, width:double.infinity, height:double.infinity,
        placeholder:(_,__)=>_fallback(name),
        errorWidget:(_,__,___)=>_fallback(name));
    }
    return _fallback(name);
  }

  @override
  Widget build(BuildContext context) {
    final Map store = widget.store;
    final double cardH = widget.cardH;
    final img = store["image"];
    final String storeName = store["store_name"]?.toString() ?? "";
    final String category  = store["category"]?.toString() ?? "";
    final String city      = store["city"]?.toString() ?? "";
    final String area      = store["area"]?.toString() ?? "";
    final int visitPts     = ((store["visit_points"] as num?)?.toInt() ?? 0);
    final bool isNew       = store["is_new_in_town"] == true;
    final String offerStr  = (store["offer"] ?? "") as String;
    final int dealCount    = ((store["deal_count"] ?? 0) as num).toInt();
    final bool hasDeal     = offerStr.isNotEmpty && dealCount > 0;
    final percentMatch     = hasDeal ? RegExp(r'([\d.]+)%').firstMatch(offerStr) : null;
    final String dealLabel = percentMatch != null ? "🔥 \${percentMatch.group(1)}% OFF" : (hasDeal ? "🔥 Deal" : "");
    final double rating    = (store["rating"] as num?)?.toDouble() ?? 0.0;

    final img2s  = store["image2"]?.toString();
    final imgs2 = (store["images"] as List?)?.map((x)=>x.toString()).toList() ?? [];
    final allImgs = [if(img!=null&&img.toString().isNotEmpty) img.toString(), if(img2s!=null&&img2s.isNotEmpty) img2s, ...imgs2];

    Widget imgWidget = allImgs.isEmpty
        ? _fallback(storeName)
        : (allImgs.length == 1
            ? _imgAt(allImgs.first, storeName)
            : PageView.builder(
                controller: _pc,
                physics: const BouncingScrollPhysics(),
                itemCount: allImgs.length,
                onPageChanged: (i) => setState(() => _imgIdx = i),
                itemBuilder: (_, i) => _imgAt(allImgs[i], storeName),
              ));

    return Container(
      height: cardH,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(28),
        boxShadow: [
          BoxShadow(color: Colors.black.withOpacity(.35), blurRadius: 22, offset: const Offset(0,8)),
        ],
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(28),
        child: Stack(
          fit: StackFit.expand,
          children: [
            // ── store image ──
            imgWidget,

            // ── dot indicator (top center) if multiple images ──
            if (allImgs.length > 1)
              Positioned(top:12, left:0, right:0,
                child: Row(mainAxisAlignment:MainAxisAlignment.center,
                  children: List.generate(allImgs.length, (i) => AnimatedContainer(
                    duration: const Duration(milliseconds:200),
                    width: i==_imgIdx ? 18 : 5, height: 5,
                    margin: const EdgeInsets.symmetric(horizontal:2),
                    decoration: BoxDecoration(
                      color: i==_imgIdx ? Colors.white : Colors.white38,
                      borderRadius: BorderRadius.circular(3)),
                  )),
                ),
              ),

            // ── dark gradient overlay bottom ──
            Positioned.fill(child: DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: [
                    Colors.transparent,
                    Colors.transparent,
                    Colors.black.withOpacity(.15),
                    Colors.black.withOpacity(.75),
                    Colors.black.withOpacity(.90),
                  ],
                  stops: const [0.0, 0.35, 0.55, 0.80, 1.0],
                ),
              ),
            )),

            // ── top-left: OFFRO LOGO overlaid on card ──
            Positioned(
              top: 16, left: 16,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal:10, vertical:6),
                decoration: BoxDecoration(
                  color: Colors.black.withOpacity(.40),
                  borderRadius: BorderRadius.circular(16),
                  border: Border.all(color: Colors.white.withOpacity(.2)),
                ),
                child: Row(mainAxisSize: MainAxisSize.min, children: [
                  buildLogo(16, kLight),
                  const SizedBox(width:5),
                  RichText(text: const TextSpan(children: [
                    TextSpan(text:"Offr", style:TextStyle(color:Colors.white,fontWeight:FontWeight.w900,fontSize:13)),
                    TextSpan(text:"O",    style:TextStyle(color:kLight,       fontWeight:FontWeight.w900,fontSize:13)),
                  ])),
                ]),
              ),
            ),

            // ── NEW IN TOWN badge removed (NIT fix 3)

            // ── deal badge moved to left (NIT fix 5) ──
            if (!isNew && hasDeal)
              Positioned(
                top: 16, left: 16,
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal:10, vertical:5),
                  decoration: BoxDecoration(
                    color: Colors.deepOrange.withOpacity(.92),
                    borderRadius: BorderRadius.circular(14),
                  ),
                  child: Text(dealLabel, style: const TextStyle(color:Colors.white,fontSize:12,fontWeight:FontWeight.w900)),
                ),
              ),

            // ── bottom info panel ──
            Positioned(
              bottom: 0, left: 0, right: 0,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 18),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    // store name
                    Text(
                      storeName,
                      style: const TextStyle(
                        color: Colors.white, fontSize: 26,
                        fontWeight: FontWeight.w900,
                        shadows: [Shadow(blurRadius:8, color:Colors.black87)],
                        letterSpacing: 0.3,
                      ),
                      maxLines: 1, overflow: TextOverflow.ellipsis,
                    ),

                    const SizedBox(height: 4),

                    // location row
                    Row(children: [
                      const Icon(Icons.location_on, color:kLight, size:13),
                      const SizedBox(width:3),
                      Text("$area${area.isNotEmpty&&city.isNotEmpty?", ":""}$city",
                        style: const TextStyle(color:Colors.white70, fontSize:12)),
                    ]),

                    const SizedBox(height: 8),

                    // rating + category row (FIX 6: category next to rating)
                    Row(children: [
                      if (rating > 0) ...[
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal:8,vertical:3),
                          decoration: BoxDecoration(
                            color: Colors.white.withOpacity(.18),
                            borderRadius: BorderRadius.circular(10),
                            border: Border.all(color:Colors.white24),
                          ),
                          child: Row(mainAxisSize:MainAxisSize.min, children:[
                            const Icon(Icons.star_rounded, color:Color(0xFFFFD700), size:13),
                            const SizedBox(width:3),
                            Text(rating.toStringAsFixed(1), style:const TextStyle(color:Colors.white,fontSize:11,fontWeight:FontWeight.w700)),
                          ]),
                        ),
                        const SizedBox(width:6),
                      ],
                      if (category.isNotEmpty)
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal:8,vertical:3),
                          decoration: BoxDecoration(
                            color: kPrimary.withOpacity(.85),
                            borderRadius: BorderRadius.circular(10),
                          ),
                          child: Text(category, style: const TextStyle(color:Colors.white,fontSize:11,fontWeight:FontWeight.w600)),
                        ),
                    ]),

                    const SizedBox(height: 6),

                    // badges row
                    Row(children: [
                      if (hasDeal && isNew)
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal:10,vertical:4),
                          decoration: BoxDecoration(
                            color: Colors.deepOrange.withOpacity(.88),
                            borderRadius: BorderRadius.circular(12),
                          ),
                          child: Text(dealLabel,
                            style: const TextStyle(color:Colors.white,fontSize:11,fontWeight:FontWeight.w700)),
                        ),
                    ]),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _fallback(String name) => Container(
    decoration: const BoxDecoration(
      gradient: LinearGradient(
        colors: [Color(0xFF2a4a40), Color(0xFF3E5F55)],
        begin: Alignment.topLeft, end: Alignment.bottomRight,
      ),
    ),
    child: Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
      const Icon(Icons.store_mall_directory_outlined, size:72, color:kLight),
      const SizedBox(height:12),
      Text(name, style: const TextStyle(color:Colors.white,fontSize:18,fontWeight:FontWeight.bold), textAlign:TextAlign.center),
    ])),
  );
}

// ─────────────────────── NEW IN TOWN BADGE ───────────────────────
class _NewInTownBadge extends StatefulWidget {
  @override State<_NewInTownBadge> createState() => _NewInTownBadgeState();
}
class _NewInTownBadgeState extends State<_NewInTownBadge> with SingleTickerProviderStateMixin {
  late AnimationController _ctrl;
  late Animation<double> _pulse;
  @override void initState() {
    super.initState();
    _ctrl = AnimationController(vsync: this, duration: const Duration(milliseconds: 1200))..repeat(reverse: true);
    _pulse = Tween<double>(begin: 0.85, end: 1.0).animate(CurvedAnimation(parent: _ctrl, curve: Curves.easeInOut));
  }
  @override void dispose() { _ctrl.dispose(); super.dispose(); }
  @override Widget build(BuildContext context) => ScaleTransition(
    scale: _pulse,
    child: Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(
        gradient: const LinearGradient(colors: [Color(0xFFFF6B35), Color(0xFFFF8C42)]),
        borderRadius: BorderRadius.circular(14),
        boxShadow: [BoxShadow(color: const Color(0xFFFF6B35).withOpacity(.5), blurRadius: 8, spreadRadius: 1)]),
      child: const Row(mainAxisSize: MainAxisSize.min, children: [
        Text("✨", style: TextStyle(fontSize: 11)),
        SizedBox(width: 3),
        Text("NEW IN TOWN", style: TextStyle(color: Colors.white, fontSize: 10, fontWeight: FontWeight.w900, letterSpacing: 0.5)),
      ])));
}

// ─────────────────────── PAYMENT SUCCESS SCREEN ───────────────────────
class PaymentSuccessScreen extends StatelessWidget {
  final String storeName;
  final String invoiceNo;
  final VoidCallback onDone;
  const PaymentSuccessScreen({super.key, required this.storeName, required this.invoiceNo, required this.onDone});

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: kBg,
    body: SafeArea(child: Center(child: Padding(
      padding: const EdgeInsets.all(32),
      child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
        Container(width: 90, height: 90,
          decoration: const BoxDecoration(color: Color(0xFF1a6640), shape: BoxShape.circle),
          child: const Icon(Icons.check_rounded, color: Colors.white, size: 52)),
        const SizedBox(height: 24),
        const Text("Payment Successful!", style: TextStyle(fontSize: 24, fontWeight: FontWeight.bold, color: kPrimary)),
        const SizedBox(height: 12),
        Text("Your store \"$storeName\" is now pending admin approval.",
          textAlign: TextAlign.center, style: const TextStyle(color: kMuted, fontSize: 15)),
        if (invoiceNo.isNotEmpty) ...[
          const SizedBox(height: 16),
          Container(padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 10),
            decoration: BoxDecoration(color: kLight.withOpacity(.5), borderRadius: BorderRadius.circular(10)),
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              const Icon(Icons.receipt_long, color: kPrimary, size: 16),
              const SizedBox(width: 6),
              Text("Invoice: $invoiceNo", style: const TextStyle(color: kPrimary, fontWeight: FontWeight.w700, fontSize: 13)),
            ])),
        ],
        const SizedBox(height: 12),
        Container(padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(color: const Color(0xFFd1f0e0), borderRadius: BorderRadius.circular(12)),
          child: const Column(children: [
            Row(children: [Icon(Icons.info_outline, size: 16, color: Color(0xFF1a6640)), SizedBox(width: 6),
              Expanded(child: Text("Admin will review and activate your store within 24 hours.",
                style: TextStyle(color: Color(0xFF1a6640), fontSize: 13)))]),
          ])),
        const SizedBox(height: 32),
        SizedBox(width: double.infinity, child: ElevatedButton(
          style: ElevatedButton.styleFrom(backgroundColor: kPrimary, padding: const EdgeInsets.symmetric(vertical: 14),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14))),
          onPressed: () {
            // Pop everything until we reach the root route (MerchantHome).
            // MerchantHome is always the first route after splash/login pushReplacement,
            // so this lands exactly on it without ever over-popping into the LoginScreen.
            Navigator.of(context).popUntil((route) => route.isFirst);
          },
          child: const Text("Back to My Stores", style: TextStyle(color: Colors.white, fontSize: 15, fontWeight: FontWeight.w700)))),
      ]),
    ))),
  );
}

// ─────────────────────── MERCHANT DEALS PAGE ───────────────────────
class MerchantDealsPage extends StatefulWidget {
  final String token;
  const MerchantDealsPage({super.key, required this.token});
  @override State<MerchantDealsPage> createState() => _MerchantDealsState();
}
class _MerchantDealsState extends State<MerchantDealsPage> {
  List<Map<String,dynamic>> _deals = []; List<Map<String,dynamic>> _stores = []; bool _loading = true;

  @override void initState() { super.initState(); _load(); }

  Future<void> _load() async {
    setState(() => _loading = true);
    _deals = List<Map<String,dynamic>>.from(await Api.getMerchantDeals(widget.token));
    _stores = List<Map<String,dynamic>>.from(await Api.getMerchantStores(widget.token));
    if (mounted) setState(() => _loading = false);
  }

  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor: kBg,
    appBar: AppBar(title: const Text("My Deals"), backgroundColor: kPrimary, foregroundColor: Colors.white),
    floatingActionButton: FloatingActionButton.extended(
      backgroundColor: kPrimary, foregroundColor: Colors.white,
      icon: const Icon(Icons.add), label: const Text("Add Deal"),
      onPressed: () {
        final activeStores = _stores.where((s) => s["status"] == "active").toList();
        if (activeStores.isEmpty) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text("You need an active store to add deals."), backgroundColor: Colors.orange));
          return;
        }
        Navigator.push(context, _route(AddDealPage(
          token: widget.token,
          storeId: activeStores[0]["_id"] ?? "",
          storeName: activeStores[0]["store_name"] ?? "",
          stores: activeStores,
        ))).then((_) => _load());
      }),
    body: _loading ? const Center(child: CircularProgressIndicator(color: kPrimary)) :
      _deals.isEmpty ? Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
        const Icon(Icons.local_offer_outlined, size: 64, color: kAccent),
        const SizedBox(height: 12),
        const Text("No deals yet", style: TextStyle(color: kMuted, fontSize: 16)),
        const SizedBox(height: 8),
        const Text("Add deals to attract more customers", style: TextStyle(color: kMuted, fontSize: 13)),
      ])) :
      RefreshIndicator(onRefresh: _load, child: ListView.builder(
        padding: const EdgeInsets.all(14),
        itemCount: _deals.length,
        itemBuilder: (_, i) {
          final d = _deals[i] as Map;
          return Card(elevation: 2, margin: const EdgeInsets.only(bottom: 10),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
            child: ListTile(
              leading: Container(width: 44, height: 44,
                decoration: BoxDecoration(color: kLight.withOpacity(.5), borderRadius: BorderRadius.circular(10)),
                child: Center(child: Text("${d['discount'] ?? 0}%",
                  style: const TextStyle(color: kPrimary, fontWeight: FontWeight.bold, fontSize: 13)))),
              title: Text(d["title"] ?? "", style: const TextStyle(fontWeight: FontWeight.w600, color: kText)),
              subtitle: Text("${d['store_name'] ?? ''} • ${d['category'] ?? ''}", style: const TextStyle(color: kMuted, fontSize: 12)),
              trailing: IconButton(
                icon: const Icon(Icons.delete_outline, color: Colors.red),
                onPressed: () async {
                  final confirm = await showDialog<bool>(context: context,
                    builder: (_) => AlertDialog(
                      title: const Text("Delete Deal?"),
                      content: Text("Delete \"${d['title']}\"?"),
                      actions: [
                        TextButton(onPressed: () => Navigator.pop(context, false), child: const Text("Cancel")),
                        ElevatedButton(style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
                          onPressed: () => Navigator.pop(context, true), child: const Text("Delete", style: TextStyle(color: Colors.white))),
                      ]));
                  if (confirm == true) {
                    await Api.deleteDeal(widget.token, d["_id"]);
                    _load();
                  }
                }),
            ));
        },
      )),
  );
}

// ─────────────────────── ADD DEAL PAGE ───────────────────────
class AddDealPage extends StatefulWidget {
  final String token; final String storeId; final String storeName;
  final List<Map<String,dynamic>> stores;
  const AddDealPage({super.key, required this.token, required this.storeId,
    required this.storeName, this.stores = const <Map<String,dynamic>>[]});
  @override State<AddDealPage> createState() => _AddDealState();
}
class _AddDealState extends State<AddDealPage> {
  final _title = TextEditingController();
  final _desc  = TextEditingController();
  final _disc  = TextEditingController();
  String _category = ""; bool _loading = false; String _msg = "";
  List<String> _categories = [];
  late String _selectedStoreId;
  late String _selectedStoreName;

  @override void initState() {
    super.initState();
    _selectedStoreId = widget.storeId;
    _selectedStoreName = widget.storeName;
    Api.fetchCategories().then((c) { if (mounted) setState(() => _categories = c); });
  }
  @override void dispose() { _title.dispose(); _desc.dispose(); _disc.dispose(); super.dispose(); }

  Future<void> _save() async {
    if (_title.text.trim().isEmpty) { setState(() => _msg = "Title required"); return; }
    if (_disc.text.trim().isEmpty)  { setState(() => _msg = "Discount % required"); return; }
    setState(() => _loading = true); _msg = "";
    try {
      await Api.addDeal(widget.token, {
        "store_id":    _selectedStoreId,
        "title":       _title.text.trim(),
        "description": _desc.text.trim(),
        "discount":    int.tryParse(_disc.text.trim()) ?? 0,
        "category":    _category,
        "start_date":  DateTime.now().toIso8601String().substring(0, 10),
        "end_date":    DateTime.now().add(const Duration(days: 30)).toIso8601String().substring(0, 10),
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text("✅ Deal added successfully!"), backgroundColor: Color(0xFF1a6640)));
      Navigator.pop(context);
    } catch (e) { setState(() => _msg = e.toString().replaceAll("Exception: ", "")); }
    if (mounted) setState(() => _loading = false);
  }

  @override Widget build(BuildContext context) => Scaffold(
    backgroundColor: kBg,
    appBar: AppBar(title: const Text("Add Deal"), backgroundColor: kPrimary, foregroundColor: Colors.white),
    body: SingleChildScrollView(padding: const EdgeInsets.all(18), child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (widget.stores.length > 1) ...[
        DropdownButtonFormField<String>(
          isExpanded: true,
          value: _selectedStoreId,
          items: widget.stores.map<DropdownMenuItem<String>>((s) =>
            DropdownMenuItem<String>(value: s["_id"]?.toString() ?? "",
              child: Text(s["store_name"]?.toString() ?? "", overflow: TextOverflow.ellipsis))).toList(),
          onChanged: (v) { if (v != null) { final s = widget.stores.firstWhere((s) => s["_id"] == v, orElse: () => <String,dynamic>{}); setState(() { _selectedStoreId = v; _selectedStoreName = s["store_name"] ?? ""; }); }},
          decoration: InputDecoration(labelText: "Store", prefixIcon: const Icon(Icons.store, color: kMuted, size: 20),
            filled: true, fillColor: Colors.white, border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)),
            enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)))),
        const SizedBox(height: 14),
      ] else
        Container(padding: const EdgeInsets.all(12), margin: const EdgeInsets.only(bottom: 14),
          decoration: BoxDecoration(color: kLight.withOpacity(.4), borderRadius: BorderRadius.circular(10)),
          child: Row(children: [const Icon(Icons.store, color: kPrimary, size: 18), const SizedBox(width: 8),
            Text(_selectedStoreName, style: const TextStyle(color: kPrimary, fontWeight: FontWeight.w600))])),
      TextField(controller: _title,
        decoration: InputDecoration(hintText: "Deal Title (e.g. 20% off on all items)", prefixIcon: const Icon(Icons.title, color: kMuted, size: 20),
          filled: true, fillColor: Colors.white, border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)),
          enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)))),
      const SizedBox(height: 12),
      TextField(controller: _disc, keyboardType: TextInputType.number,
        decoration: InputDecoration(hintText: "Discount %", prefixIcon: const Icon(Icons.percent, color: kMuted, size: 20),
          filled: true, fillColor: Colors.white, border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)),
          enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)))),
      const SizedBox(height: 12),
      DropdownButtonFormField<String>(
        isExpanded: true,
        value: _category.isEmpty ? null : _category,
        items: _categories.map((c) => DropdownMenuItem<String>(value: c, child: Text(c))).toList(),
        onChanged: (v) => setState(() => _category = v ?? ""),
        decoration: InputDecoration(hintText: "Category", prefixIcon: const Icon(Icons.category, color: kMuted, size: 20),
          filled: true, fillColor: Colors.white, border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)),
          enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)))),
      const SizedBox(height: 12),
      TextField(controller: _desc, maxLines: 3,
        decoration: InputDecoration(hintText: "Description (optional)", prefixIcon: const Icon(Icons.description_outlined, color: kMuted, size: 20),
          filled: true, fillColor: Colors.white, border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)),
          enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: kBorder)))),
      const SizedBox(height: 20),
      SizedBox(height: 50, child: ElevatedButton(
        onPressed: _loading ? null : _save,
        style: ElevatedButton.styleFrom(backgroundColor: kPrimary, shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14))),
        child: _loading ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2))
          : const Text("Add Deal", style: TextStyle(color: Colors.white, fontSize: 15, fontWeight: FontWeight.w700)))),
      if (_msg.isNotEmpty) ...[const SizedBox(height: 10),
        Text(_msg, textAlign: TextAlign.center, style: TextStyle(color: Colors.red.shade700, fontSize: 13))],
      const SizedBox(height: 24),
    ])),
  );
}
