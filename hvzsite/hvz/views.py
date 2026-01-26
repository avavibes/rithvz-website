from itertools import chain
import json
import os
from functools import lru_cache
from itertools import chain

import discord
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import Group
from django.core import exceptions
from django.db.models import Q, Count
from django.db.models.functions import Lower
from django.db.utils import IntegrityError
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect
from django.shortcuts import render, redirect
from hvzsite.settings import MEDIA_ROOT, STATIC_ROOT
from rest_framework import permissions, viewsets
from rest_framework.decorators import api_view
from rest_framework.views import APIView
from rest_framework_api_key.permissions import HasAPIKey

from .forms import ReportForm
from .models import About, Announcement, AntiVirus, BadgeInstance, Blaster, BodyArmor, Clan, ClanHistoryItem, \
    CustomRedirect, DiscordLinkCode, FailedAVAttempt, Mission, PlayerStatus, Person, Report, Rules, Scoreboard, Tag
from .models import get_active_game
from .serializers import GroupSerializer, UserSerializer

if settings.DISCORD_REPORT_WEBHOOK_URL:
    report_webhook = discord.SyncWebhook.from_url(settings.DISCORD_REPORT_WEBHOOK_URL)

    
def for_all_methods(decorator):
    def decorate(cls):
        for attr in cls.__dict__: # there's propably a better way to do this
            if callable(getattr(cls, attr)):
                setattr(cls, attr, decorator(getattr(cls, attr)))
        return cls
    return decorate


@lru_cache(maxsize=1)
def get_recent_events(most_recent_tag, most_recent_av, most_recent_registration):
    game = get_active_game()
    humancount = PlayerStatus.objects.filter(Q(game=game) & (Q(status='h') | Q(status='v') | Q(status='e'))).count()
    zombiecount = PlayerStatus.objects.filter(Q(game=game) & (Q(status='z') | Q(status='x') | Q(status='o'))).count()
    most_tags = PlayerStatus.objects.filter(game=game).annotate(tag_count=Count("player__taggers", filter=Q(player__taggers__game=game))).filter(tag_count__gt=0).order_by("-tag_count")
    recent_tags = Tag.objects.filter(game=get_active_game()).order_by('-timestamp')
    recent_avs = AntiVirus.objects.filter(game=get_active_game(), used_by__isnull=False).order_by('-time_used')
    merged_recents = list(chain(recent_avs, recent_tags))
    merged_recents.sort(key=lambda x:x.get_timestamp, reverse=True)   
    starting_zombie_count = PlayerStatus.objects.filter(game=game, status='o').count()
    starting_human_count = PlayerStatus.objects.filter(game=game, status__in=['h','v','e','z','x']).count()
    running_zombie_count = starting_zombie_count
    running_human_count = starting_human_count
    timestamps = [game.start_date_chart_js]
    zombiecounts = [starting_zombie_count]
    humancounts = [starting_human_count]
    for index in range(len(merged_recents)-1,-1,-1):
        item = merged_recents[index]
        if isinstance(item, Tag):
            if isinstance(item.taggee,  Person):
                running_zombie_count += 1
                running_human_count -= 1
                timestamps.append(item.timestamp_chart_js)
                zombiecounts.append(running_zombie_count)
                humancounts.append(running_human_count)
        elif isinstance(item, AntiVirus):
            running_zombie_count -= 1
            running_human_count += 1
            timestamps.append(item.timestamp_chart_js)
            zombiecounts.append(running_zombie_count)
            humancounts.append(running_human_count)
    return (humancount, zombiecount, most_tags, merged_recents, timestamps, zombiecounts, humancounts)


def index(request):
    game = get_active_game()
    if len(most_recent_tags := Tag.objects.filter(game=game).order_by('-timestamp')) > 0:
        most_recent_tag = most_recent_tags[0]
    else:
        most_recent_tag = None
    if len(most_recent_avs := AntiVirus.objects.filter(game=game, used_by__isnull=False).order_by('-time_used')) > 0:
        most_recent_av = most_recent_avs[0]
    else:
        most_recent_av = None
    if len(most_recent_registrations := PlayerStatus.objects.filter(game=game).order_by("-activation_timestamp")) > 0:
        most_recent_registration = most_recent_registrations[0]
    else:
        most_recent_registration = None
    (humancount, zombiecount, most_tags, merged_recents, timestamps, zombiecounts, humancounts) = get_recent_events(most_recent_tag, most_recent_av, most_recent_registration)

    scoreboards = Scoreboard.objects.filter(associated_game=game, active=True)

    return render(request, "index.html", {'game': game,
                                          'humancount': humancount,
                                          'zombiecount': zombiecount,
                                          'most_tags': most_tags[:10],
                                          'recent_events': merged_recents[0:10],
                                          'timestamps': timestamps,
                                          'zombiecounts': zombiecounts,
                                          'humancounts': humancounts,
                                          'scoreboards': scoreboards})


def infection(request):
    game = get_active_game()
    ozs = PlayerStatus.objects.filter(game=game, status='o')
    tags = Tag.objects.filter(game=game)
    return render(request, "infection.html", {'ozs':ozs, 'tags':tags})


class UserViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows users to be viewed or edited.
    """
    queryset = Person.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]


class GroupViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows groups to be viewed or edited.
    """
    queryset = Group.objects.all()
    serializer_class = GroupSerializer
    permission_classes = [permissions.IsAuthenticated]


def player_view(request, player_id, game=None, discord_code=None):
    player = Person.objects.get(player_uuid=player_id)
    if game is None:
        game = get_active_game()

    context = {
        'user': request.user,
        'player': player,
        'badges': BadgeInstance.objects.filter(player=player), 
        'tags': Tag.objects.filter(tagger=player, game=game),
        'status': PlayerStatus.objects.get_or_create(player=player, game=game)[0],
        'blasters': Blaster.objects.filter(owner=player, game_approved_in=game),
        'domain': request.build_absolute_uri('/tag/'),
        'discord_code': discord_code,
        'reportees': Report.objects.filter(reportees__exact=player),
        'reporters': Report.objects.filter(reporter=player),
        'failedavs': FailedAVAttempt.objects.filter(player=player, game=game),
        'is_user_clan_leader': Clan.objects.filter(leader=request.user).count() > 0 if request.user.is_authenticated else False,
        'is_player_clan_leader': Clan.objects.filter(leader=player).count() > 0
    }
    
    return render(request, "player.html", context)


def redirect_view(request, redir_name):
    try:
        redir_target = CustomRedirect.objects.get(redirect_name=redir_name)
    except:
        return HttpResponse(status=400, content=f'Given redirect string "{redir_name}" does not exist')

    return redirect(redir_target.target)


def clan_view(request, clan_name):
    clan = Clan.objects.get(name=clan_name)
    is_leader = (request.user.is_authenticated and clan.leader == request.user)
    if is_leader or (request.user.is_authenticated and request.user.admin_this_game):
        history = ClanHistoryItem.objects.filter(clan=clan).order_by('-timestamp')
    else:
        history = []
    can_join = request.user.is_authenticated and Clan.objects.filter(leader=request.user).count() == 0 and request.user.has_ever_played and clan.leader is not None
    context = {
        'clan': clan,
        'roster': Person.objects.filter(clan=clan),
        'is_leader': is_leader,
        'user': request.user,
        'history': history,
        'can_join': can_join,
        'show_history': is_leader or (request.user.is_authenticated and request.user.admin_this_game)
    }
    return render(request, "clan.html", context)


def players(request):
    context = {}
    return render(request, "players.html", context)


@api_view(["GET"])
def players_api(request, game=None):
    if game is None:
        game = get_active_game()
    r = request.query_params
    try:
        order_column = int(r.get("order[0][column]"))
        assert order_column in [1,2,3,4]
        order_column_name = r.get(f"columns[{order_column}][name]")
        assert order_column_name in ("name","status","tags","clan")
        order_direction = r.get("order[0][dir]")
        assert order_direction in ("asc","desc")
        limit = int(request.query_params["length"])
        start = int(request.query_params["start"])
        search = r["search[value]"] 
    except AssertionError:
        raise
    query = Person.full_name_objects.filter(playerstatus__game=game, playerstatus__status__in=['h','v','e','z','o','x','a','m'])
    if search != "":
        query = query.filter(Q(first_name__icontains=search) | Q(last_name__icontains=search) | Q(clan__name__icontains=search))
    if order_column_name == 'tags':
        query = query.annotate(n_tags=Count('taggers', filter=Q(taggers__game=game))).order_by(f"""{'-' if order_direction == 'asc' else ''}n_tags""")
    elif order_column_name == 'status':
        query = sorted([person for person in query], key=lambda person: person.current_status.listing_priority, reverse=(order_direction=='desc'))
    else:
        if order_direction == 'desc':
            query = query.order_by(Lower(f"""{ {"name":"full_name", "clan": "clan__name"}[order_column_name]}""").desc())
        else:
            query = query.order_by(Lower(f"""{ {"name":"full_name", "clan": "clan__name"}[order_column_name]}""").asc())

    result = []
    filtered_length = len(query)
    if start < filtered_length:
        for person in query[start:]:
            if limit == 0:
                break
            try:
                person_status = PlayerStatus.objects.get(player=person, game=game)
            except:
                continue
            result.append({
                "name": f"""<a class="dt_name_link" href="/player/{person.player_uuid}/">{person.readable_name(request.user.is_authenticated and request.user.active_this_game)}</a>""",
                "pic": f"""<a class="dt_profile_link" href="/player/{person.player_uuid}/"><img src='{person.picture_url}' class='dt_profile' /></a>""",
                "status": {"h": "Human", "a": "Admin", "z": "Zombie", "m": "Mod", "v": "Human", "o": "Zombie", "n": "NonPlayer", "x": "Zombie", "e": "Human (Extracted)"}[person_status.status],
                "clan": None if person.clan is None else (f"""<a href="/clan/{person.clan.name}/" class="dt_clan_link">person.clan.name</a>""" if (person.clan is None or person.clan.picture is None) else f"""<a href="/clan/{person.clan.name}/" class="dt_clan_link"><img src='{person.clan.picture.url}' class='dt_clanpic' alt='{person.clan}' /><span class="dt_clanname">{person.clan}</span></a>"""),
                "clan_pic": None if (person.clan is None or person.clan.picture is None) else person.clan.picture.url,
                "tags": Tag.objects.filter(tagger=person,game=game).count(),
                "DT_RowClass": {"h": "dt_human", "v": "dt_human", "e": "dt_human", "a": "dt_admin", "z": "dt_zombie", "o": "dt_zombie", "n": "dt_nonplayer", "x": "dt_zombie", "m": "dt_mod"}[person_status.status],
                "DT_RowData": {"person_url": f"/player/{person.player_uuid}/", "clan_url": f"/clan/{person.clan.name}/" if person.clan is not None else ""}
            })
            limit -= 1
    data = {
        "draw": int(r['draw']),
        "recordsTotal": Person.full_name_objects.filter(playerstatus__game=game, playerstatus__status__in=['h','v','e','z','o','x','a','m']).count(),
        "recordsFiltered": filtered_length,
        "data": result
    }
    return JsonResponse(data)


def clans(request):
    context = {"clans" : Clan.objects.all()}
    return render(request, "clans.html", context)


def rules(request):
    return render(request, "rules.html", {'rules': Rules.load()})


def about(request):
    return render(request, "about.html", {'about': About.load()})


def create_report(request):
    report_complete = False
    report_id = None
    if request.method == "POST":
        form = ReportForm(request.POST, request.FILES, authenticated=request.user.is_authenticated)
        if form.is_valid():
            report = form.instance
            report.game = get_active_game()
            if request.user.is_authenticated:
                report.reporter = request.user
            report.status = "n"
            report.save()
            report_complete = True
            report_id = report.report_uuid
            form = ReportForm(authenticated=request.user.is_authenticated)
            if report_webhook:
                report_webhook.send("!report \n" +
                                    json.dumps({
                                        'report_text': report.report_text, 
                                        'reporter_email': report.reporter_email,
                                        'reporter': str(report.reporter),
                                        'timestamp': str(report.timestamp),
                                        # 'picture' = models.ImageField(upload_to='report_images/', null=True, blank=True)
                                    }, indent=2))
        else:
            messages.error(request, "Unsuccessful report. Invalid information.")
    else:
        form = ReportForm(authenticated=request.user.is_authenticated)
    return render(request=request, template_name="create_report.html", context={"form":form, "reportcomplete":report_complete, "report_id": report_id})


## REST API endpoints

class ApiDiscordId(APIView):
    """
    Returns the player UUID associated with the given discord ID.

    @param id The discord id to be checked
    @return {
      discord-id: The input id
      player-id: The UUID of the player
      player-name: The full name of the player
    }
    """
    permission_classes = [HasAPIKey]

    def get(self, request):
        r = request.query_params

        if 'id' not in r:
            return HttpResponse(status=400, content='Missing field: "id"')
        discord_id = r.get('id')

        try:
            player = Person.objects.get(discord_id=discord_id)
        except Person.DoesNotExist:
            return HttpResponse(status=404, content='No player with the given discord id')

        data = {
            'discord-id': discord_id,
            'player-id': player.player_uuid,
            'player-name': str(player)
        }
        return JsonResponse(data)


class ApiLinkDiscordId(APIView):
    permission_classes = [HasAPIKey]

    def get(self, request):
        r = request.query_params

        if 'discord_id' not in r:
            return HttpResponse(status=400, content='Missing field: "discord_id"')
        if 'link_code' not in r:
            return HttpResponse(status=400, content='Missing field: "link_code"')
        discord_id = r.get('discord_id')
        link_code = r.get('link_code')

        try:
            code = DiscordLinkCode.objects.get(code=link_code)
        except DiscordLinkCode.DoesNotExist:
            return HttpResponse(status=404, content='Bad link code: does not exist')

        code.account.discord_id = discord_id
        code.account.save()
        code.delete()

        return HttpResponse('Successfully linked account')

    
class ApiMissions(APIView):
    permission_classes = [HasAPIKey]

    def get(self, request):
        r = request.query_params

        if 'team' not in r:
            return HttpResponse(status=400, content='Missing field: "team"')
        team = r.get('team')

        valid_teams = ['Human', 'Zombie', 'Staff']
        if team not in valid_teams:
            return HttpResponse(status=400, content='Invalid team, must be one of: '+str(valid_teams))

        missions = Mission.objects.filter(team__in=[team[0].lower(),'a'], game=get_active_game())

        data = {
            'missions': [
                {
                    'story-form': m.story_form,
                    'story-form-live-time': m.story_form_go_live_time,
                    'mission-text': m.mission_text,
                    'mission-text-live-time': m.go_live_time,
                }
                for m in missions]
        }
        return JsonResponse(data)

class ApiPlayerId(APIView):
    permission_classes = [HasAPIKey]

    def get(self, request):
        r = request.query_params

        if 'uuid' in r:
            player_id = r.get('uuid')
            try:
                player = Person.objects.get(player_uuid=player_id)
            except Person.DoesNotExist:
                return HttpResponse(status=404, content='No player with the given user id')

        elif 'zid' in r:
            try:
                pstatus = PlayerStatus.objects.get(zombie_uuid=r.get('zid'), game=get_active_game())
            except PlayerStatus.DoesNotExist:
                return HttpResponse(status=404, content='No player with the given zombie id')
            player = pstatus.player
        else:
            return HttpResponse(status=400, content='Missing required field: Either "uuid" or "zid" necessary to fulfill this request')

        data = {
            'uuid': player.player_uuid,
            'clan': player.clan.clan_uuid,
            'email': player.email,
            'name': player.readable_name(True),
            'status': player.current_status.status,
            'tags': player.current_status.num_tags,
        }
        return JsonResponse(data)

class ApiClans(APIView):
    def get(self, request):
        t = list(Clan.objects.values_list('name', flat=True))

        data = {
            'clans': t
        }
        return JsonResponse(data)


class ApiPlayers(APIView):
    '''
    Returns all player information
    '''
    def get(self, request):
        game = get_active_game()
        players = [
            {
                'name': p.readable_name(request.user.is_authenticated and request.user.active_this_game),
                'id': p.player_uuid,
                'status': p.current_status.get_status_display(),
                'tags': p.current_status.num_tags,
            } for p in Person.full_name_objects.filter(playerstatus__game=game, playerstatus__status__in=['h','v','e','z','o','x','a','m'])
        ]

        data = {
            'players': players
        }
        return JsonResponse(data)


class ApiTag(APIView):
    permission_classes = [HasAPIKey]

    def post(self, request):
        r = request.query_params

        if 'tagger' not in r:
            return HttpResponse(status=400, content='Missing field: "tagger"')
        if 'taggee' not in r:
            return HttpResponse(status=400, content='Missing field: "taggee"')

        try:
            tagger = Person.objects.get(player_uuid=r['tagger'])
        except Person.DoesNotExist:
            return HttpResponse(status=404, content='No player with the given tagger id')

        try:
            taggee = PlayerStatus.objects.get(tag1_uuid=r['taggee'])
            if taggee.status == 'h':
                taggee.status = 'z'
            else:
                return HttpResponse(status=400, content='Invalid status, ensure the taggee ID is correct')
        except PlayerStatus.DoesNotExist:
            try:
                taggee = PlayerStatus.objects.get(tag2_uuid=r['taggee'])
                if taggee.status == 'v':
                    taggee.status = 'x'
                else:
                    return HttpResponse(status=400, content='Invalid status, ensure the taggee ID is correct')
            except PlayerStatus.DoesNotExist:
                return HttpResponse(status=404, content='No player with the given taggee id')

        
        tag = Tag.objects.create(tagger=tagger, taggee=taggee.player, game=get_active_game())
        taggee.save()
        tag.save()

        return HttpResponse(status=200)


class ApiReports(APIView):
    permission_classes = [HasAPIKey]

    def get(self, request):
        data = {
            'reports': [
                {
                    "report-text": report.report_text,
                    "reporter-email": report.reporter_email,
                    "reporter": report.reporter.readable_name(True) if report.reporter else None,
                    "timestamp": report.timestamp,
                    "status": report.status,
                }
                for report in Report.objects.all()],
        }
        return JsonResponse(data)


class ApiCreateAv(APIView):
    permission_classes = [HasAPIKey]

    def post(self, request):
        r = request.query_params

        if 'exp-time' not in r:
            return HttpResponse(status=400, content='Missing field: "exp-time"')

        try:
            if 'av-code' in r:
                av = AntiVirus.objects.create(av_code=r['av-code'], game=get_active_game(), expiration_time = r['exp-time'])
            else:
                av = AntiVirus.objects.create(game=get_active_game(), expiration_time = r['exp-time'])
        except exceptions.ValidationError:
            return HttpResponse(status=400, content='Invalid time format. It must be in YYYY-MM-DD HH:MM[:ss[.uuuuuu]][TZ] format.')
        except IntegrityError:
            return HttpResponse(status=400, content='Cannot create duplicate AV code.')

        av.save()
        
        return HttpResponse('Successfully created AV: "{}"'.format(av.av_code))

class ApiCreateBodyArmor(APIView):
    permission_classes = [HasAPIKey]

    def post(self, request):
        r = request.query_params

        if 'exp-time' not in r:
            return HttpResponse(status=400, content='Missing field: "exp-time"')

        try:
            if 'armor-code' in r:
                armor = BodyArmor.objects.create(armor_code=r['armor-code'], game=get_active_game(), expiration_time = r['exp-time'])
            else:
                armor = BodyArmor.objects.create(game=get_active_game(), expiration_time = r['exp-time'])
        except exceptions.ValidationError:
            return HttpResponse(status=400, content='Invalid time format. It must be in YYYY-MM-DD HH:MM[:ss[.uuuuuu]][TZ] format.')
        except IntegrityError:
            return HttpResponse(status=400, content='Cannot create duplicate BodyArmor code.')

        armor.save()

        return HttpResponse('Successfully created Body Armor: "{}"'.format(armor.armor_code))
    

def view_tags(request):
    tags = Tag.objects.filter(game=get_active_game()).order_by("-timestamp")
    return render(request, "tags_user.html", {'tags':tags})

def profile_picture_view(request, player_uuid, fname):
    if request.user.is_authenticated or Person.objects.get(player_uuid=player_uuid).admin_this_game:
        new_url = f'{os.path.split(MEDIA_ROOT)[0]}{request.path}'
    else:
        new_url = f'{STATIC_ROOT}/images/noprofile.png'
    return HttpResponse(open(new_url, "rb"))


def view_announcement(request, announcement_id):
    try:
        announcement = Announcement.objects.get(id=announcement_id)
        return render(request, "announcement.html", {'announcement':announcement})
    except:
        return HttpResponseRedirect("/")
